"""Non-executable local subset of the Claude plugin package format.

Reference: https://code.claude.com/docs/en/plugins-reference . A package has
`.claude-plugin/plugin.json`, conventional `skills/<name>/SKILL.md`, optional
Markdown/JSON skill resources, and remote HTTP MCP declarations. Everything is
validated in memory; ZIP members are never extracted or executed. UTF-8 BOMs
and CRLF line endings are normalized to BOM-free LF text; the archive digest
still hashes the exact uploaded ZIP bytes. Bare CR and other controls remain
invalid. This is an explicit subset, not a Claude Code runtime or a malware/
content safety scanner.
"""
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib


ARCHIVE_LIMIT = 4 * 1024 * 1024
EXPANDED_LIMIT = 4 * 1024 * 1024
FILE_LIMIT = 256 * 1024
SKILL_LIMIT = 64 * 1024
FILE_COUNT = 256
SKILL_COUNT = 32
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
META_KEYS = frozenset({"$schema", "name", "displayName", "version", "description", "author",
                       "homepage", "repository", "license", "keywords", "defaultEnabled"})
MANIFEST_KEYS = META_KEYS | {"skills", "mcpServers"}
FRONT_KEYS = frozenset({"name", "description", "user-invocable", "disable-model-invocation", "argument-hint"})
RESERVED = {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]}


class PluginError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def text(value, label, limit, *, empty=False):
    if (not isinstance(value, str) or len(value.encode("utf-8", errors="surrogatepass")) > limit
            or (not empty and not value.strip()) or any(ord(char) < 32 and char not in "\n\t" for char in value)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise PluginError("invalid_package", "Invalid " + label)
    return value


def identifier(value, label="component name"):
    if not isinstance(value, str) or len(value) > 64 or NAME.fullmatch(value) is None:
        raise PluginError("invalid_package", "Invalid " + label)
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PluginError("invalid_package", "Duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("Non-finite JSON number")


def json_object(raw, label):
    try:
        result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise PluginError("invalid_package", "Invalid " + label + " JSON") from None
    if not isinstance(result, dict):
        raise PluginError("invalid_package", label + " must be a JSON object")
    return result


def _path(name):
    if not isinstance(name, str) or not name or len(name) > 240 or not re.fullmatch(r"[A-Za-z0-9._/ -]+", name):
        raise PluginError("invalid_package", "Unsupported package path")
    parts = name.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " "))
           or part.split(".", 1)[0].casefold() in RESERVED for part in parts):
        raise PluginError("invalid_package", "Unsafe package path")
    return name


def read_archive(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > ARCHIVE_LIMIT:
        raise PluginError("package_limit", "Plugin ZIP exceeds the upload limit", 413)
    files, names, directories, spellings, total = {}, set(), set(), {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > FILE_COUNT:
                raise PluginError("package_limit", "Too many plugin archive entries", 413)
            for info in members:
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise PluginError("invalid_package", "Encrypted or unsupported ZIP member")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise PluginError("invalid_package", "Plugin links and special files are not supported")
                if not info.is_dir() and mode & 0o111:
                    raise PluginError("unsupported_package", "Executable package members are not supported")
                if info.external_attr & 0x400:
                    raise PluginError("invalid_package", "Plugin reparse points are not supported")
                name = _path(info.filename[:-1] if info.is_dir() else info.filename)
                folded = name.casefold()
                for index in range(1, len(name.split("/")) + 1):
                    prefix = "/".join(name.split("/")[:index])
                    previous = spellings.setdefault(prefix.casefold(), prefix)
                    if previous != prefix:
                        raise PluginError("invalid_package", "Case-colliding package directory")
                if folded in names:
                    raise PluginError("invalid_package", "Duplicate or case-colliding package path")
                names.add(folded)
                if info.is_dir():
                    directories.add(folded)
                    continue
                total += info.file_size
                if info.file_size > FILE_LIMIT or total > EXPANDED_LIMIT:
                    raise PluginError("package_limit", "Expanded plugin content exceeds its limit", 413)
                if info.file_size > 16384 and info.file_size > max(1, info.compress_size) * 100:
                    raise PluginError("package_limit", "Excessive ZIP compression ratio", 413)
                # The central directory is untrusted. Also bound the actual read.
                with archive.open(info) as source:
                    content = source.read(FILE_LIMIT + 1)
                if len(content) != info.file_size or len(content) > FILE_LIMIT:
                    raise PluginError("package_limit", "Plugin member size mismatch", 413)
                try:
                    files[name] = content.decode("utf-8-sig").replace("\r\n", "\n")
                except UnicodeError:
                    raise PluginError("invalid_package", "Plugin files must contain UTF-8 text") from None
            file_names = {name.casefold() for name in files}
            for name in names:
                parents = ["/".join(name.split("/")[:index]) for index in range(1, len(name.split("/")))]
                if any(parent in file_names for parent in parents) or (name in file_names and name in directories):
                    raise PluginError("invalid_package", "Package file/directory path conflict")
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError, ValueError, zlib.error) as error:
        if isinstance(error, PluginError):
            raise
        raise PluginError("invalid_package", "Invalid plugin ZIP") from None
    return files


def _scalar(value):
    value = value.strip()
    if not value or value.startswith(("!", "&", "*", "[", "{", "|", ">")):
        raise PluginError("unsupported_package", "Unsupported SKILL frontmatter value")
    if value.startswith('"'):
        try:
            result = json.loads(value)
        except ValueError:
            raise PluginError("invalid_package", "Invalid quoted SKILL value") from None
        if not isinstance(result, str):
            raise PluginError("invalid_package", "SKILL text value must be a string")
        return result
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise PluginError("invalid_package", "Invalid quoted SKILL value")
        return value[1:-1].replace("''", "'")
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if re.search(r"\s[&*!]|:\s", value):
        raise PluginError("unsupported_package", "YAML structures are not supported in SKILL metadata")
    return value


def parse_skill(raw, directory):
    if isinstance(raw, str):
        raw = raw.removeprefix("\ufeff").replace("\r\n", "\n")
    text(raw, "SKILL.md", SKILL_LIMIT)
    lines = raw.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise PluginError("invalid_package", "SKILL.md requires YAML frontmatter")
    end = lines.index("---", 1)
    metadata, index = {}, 1
    while index < end:
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z-]*):\s*(.*)", line)
        if match is None or match[1] not in FRONT_KEYS:
            raise PluginError("unsupported_package", "Unsupported SKILL frontmatter field or YAML syntax")
        key, value = match.groups()
        if key in metadata:
            raise PluginError("invalid_package", "Duplicate SKILL frontmatter field")
        if value in ("|", "|-", ">", ">-"):
            block = []
            while index < end and (not lines[index].strip() or lines[index].startswith("  ")):
                block.append(lines[index][2:] if lines[index].startswith("  ") else "")
                index += 1
            value = ("\n" if value.startswith("|") else " ").join(block).strip()
        else:
            value = _scalar(value)
        if key in {"user-invocable", "disable-model-invocation"}:
            if value not in ("true", "false"):
                raise PluginError("invalid_package", "SKILL invocation flags must be boolean")
            value = value == "true"
        metadata[key] = value
    name = identifier(metadata.get("name"), "skill name")
    if name != directory:
        raise PluginError("invalid_package", "SKILL name must match its directory")
    description = text(metadata.get("description"), "skill description", 4096)
    instructions = text("\n".join(lines[end + 1:]).strip(), "skill instructions", SKILL_LIMIT)
    return {"name": name, "description": description, "instructions": instructions,
            "user_invocable": metadata.get("user-invocable", True),
            "model_invocable": not metadata.get("disable-model-invocation", False),
            "argument_hint": text(metadata.get("argument-hint", ""), "argument hint", 256, empty=True),
            "resources": {}}


def _mcp(declarations, catalog):
    if not isinstance(declarations, dict) or len(declarations) > 32:
        raise PluginError("invalid_package", "Invalid MCP declarations")
    result = {}
    for name, server in declarations.items():
        identifier(name, "MCP server name")
        if not isinstance(server, dict) or set(server) - {"type", "url"} or server.get("type", "http") != "http":
            raise PluginError("unsupported_package", "Only credential-free official HTTP MCP references are supported")
        url = server.get("url")
        matches = [ident for ident, entry in catalog.items() if entry.get("url") == url and isinstance(url, str)]
        if len(matches) != 1:
            raise PluginError("unsupported_package", "MCP URL is not an exact reviewed catalog endpoint")
        result[name] = {"catalog_id": matches[0], "url": url}
    return result


def validate_files(files, catalog):
    """Return normalized text data; refuse unsupported components, never execute them."""
    if ".claude-plugin/plugin.json" not in files:
        raise PluginError("invalid_package", "Missing .claude-plugin/plugin.json at ZIP root")
    manifest = json_object(files[".claude-plugin/plugin.json"], "plugin manifest")
    if set(manifest) - MANIFEST_KEYS:
        raise PluginError("unsupported_package", "Unsupported plugin manifest fields: " + ", ".join(sorted(set(manifest) - MANIFEST_KEYS)))
    identifier(manifest.get("name"), "plugin name")
    for field, limit in (("displayName", 256), ("version", 128), ("description", 4096), ("homepage", 2048),
                         ("repository", 2048), ("license", 128), ("$schema", 2048)):
        if field in manifest:
            text(manifest[field], "manifest " + field, limit)
    if "defaultEnabled" in manifest and type(manifest["defaultEnabled"]) is not bool:
        raise PluginError("invalid_package", "defaultEnabled must be boolean")
    if "author" in manifest:
        author = manifest["author"]
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"} or "name" not in author:
            raise PluginError("invalid_package", "Invalid plugin author")
        for field, value in author.items():
            text(value, "author " + field, 512)
    if "keywords" in manifest:
        keywords = manifest["keywords"]
        if not isinstance(keywords, list) or len(keywords) > 32:
            raise PluginError("invalid_package", "Invalid plugin keywords")
        for value in keywords:
            text(value, "plugin keyword", 64)
    # A deliberately finite path profile; custom executable components are not
    # silently ignored under the guise of standard Claude Code compatibility.
    if "skills" in manifest and manifest["skills"] not in ("./skills", "./skills/", ["./skills"], ["./skills/"]):
        raise PluginError("unsupported_package", "Only conventional ./skills/ package paths are supported")
    declarations = {}
    if ".mcp.json" in files:
        config = json_object(files[".mcp.json"], "MCP configuration")
        if "mcpServers" in config:
            if set(config) != {"mcpServers"}:
                raise PluginError("invalid_package", "MCP wrapper has unsupported fields")
            config = config["mcpServers"]
        declarations.update(_mcp(config, catalog))
    inline = manifest.get("mcpServers")
    if inline is not None:
        if inline == "./.mcp.json":
            if ".mcp.json" not in files:
                raise PluginError("invalid_package", "Declared .mcp.json is missing")
        else:
            parsed = _mcp(inline, catalog)
            if declarations.keys() & parsed.keys():
                raise PluginError("invalid_package", "Duplicate MCP server declaration")
            declarations.update(parsed)
    skills, resources = {}, {}
    for name, content in files.items():
        text(content, "plugin text file", FILE_LIMIT, empty=True)
        if name in {".claude-plugin/plugin.json", ".mcp.json", "README.md", "LICENSE", "LICENSE.md"}:
            continue
        parts = name.split("/")
        if len(parts) < 3 or parts[0] != "skills":
            raise PluginError("unsupported_package", "Only plugin metadata and Markdown/JSON skill files are supported")
        skill_name = identifier(parts[1], "skill directory")
        if parts[2:] == ["SKILL.md"]:
            skills[skill_name] = parse_skill(content, skill_name)
        else:
            if len(parts) == 3 or not (name.endswith(".md") or name.endswith(".json")):
                raise PluginError("unsupported_package", "Skill resources must be Markdown or JSON in a subdirectory")
            if name.endswith(".json"):
                json_object(content, "skill resource")
            resources.setdefault(skill_name, {})["/".join(parts[2:])] = content
    if not skills and not declarations:
        raise PluginError("invalid_package", "Plugin has no supported skills or MCP references")
    if len(skills) > SKILL_COUNT:
        raise PluginError("package_limit", "Too many plugin skills", 413)
    if set(resources) - set(skills):
        raise PluginError("invalid_package", "Skill resource has no owning SKILL.md")
    for name, skill in skills.items():
        skill["resources"] = dict(sorted(resources.get(name, {}).items()))
    return {"manifest": {key: manifest[key] for key in sorted(manifest) if key in META_KEYS},
            "skills": [skills[key] for key in sorted(skills)], "mcp_servers": declarations}


def parse_archive(raw, catalog):
    package = validate_files(read_archive(raw), catalog)
    return package, hashlib.sha256(raw).hexdigest()
