# Local Make formats

The local catalog supports `page` (UTF-8 HTML), `document` (DOCX), `deck`
(PPTX), and `sheet` (XLSX), plus five functional HTML families: `dashboard`,
`design`, `animation`, `whiteboard`, and `checklist`. Office families appear only when their installed
writer library is available. The package writers are python-docx 1.2.0,
python-pptx 1.0.2, and openpyxl 3.1.5. Original hosted template content and
template IDs were not recovered; the local catalog supplies no invented
vendor listings. The five web families are explicitly HTML applications, not
Office-format substitutes. Their starters differ in behavior: entered-data
charts and tables, an editable visual poster, controlled animation, a movable
note board, and editable native checkboxes. They do not claim to reproduce an
unknown hosted template.

The original `frame/types` request returns stable local UUID type IDs and original
native `icon_key` values: `file`, `slides`, and `spreadsheet`. In the inspected
Android source, `gqb` constructs native entries and `kmd0.k` maps those icons
to document/deck/sheet families. `frame/types/{type}/create` creates a real
title-only starter package, preserving the original create response
`{"slug": "<UUID>"}`. It is an empty starting document, never claimed to be
completion of a user's task. Idempotency keys cannot be reused across formats.

## Native type-ID wire contract

The family names above are internal semantic names and backwards-compatible
input aliases, not valid native type slugs. Original `pxn.java:43,94` uses
`wyr` (`CodeArtifactId`) for `ArtifactTypeListing.slug`. `wyr.java:5`,
`drl.java:281–291`, `ktz.java:5` and `kll.e` require the exact lowercase UUID
syntax `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`.
The original `vxn` response uses `rxn`, a tolerant list serializer; an HTTP
200 and non-null `wxn` can therefore still contain zero usable rows after
invalid semantic slugs are discarded. Acceptance must check parsed row count
and usable entries, not just generic object decode success.

Adjacent serializers were checked separately: `onj.java:28–31,56–67` also
uses nullable `CodeArtifactId` for both `AiboTaskMake.artifact_type_id` and its
existing `page` artifact ID. `r8i.java:63,161` uses the same ID serializer for
gallery artifact `id`. However `r8i.java:101,226–228` encodes gallery
`type_slug` with `v990`, an ordinary nullable string; that field is **not**
itself UUID-validated. We still return its matching catalog UUID so that the
catalog, source and type selection consistently refer to one type identity.
Artifact UUIDs are distinct from type UUIDs and continue to refer to the
actual immutable bytes.

The following UUIDv5 identifiers are owned by this local catalog. They are
derived from the fixed URL namespace and `duet-local-make-type:{family}`,
independent of account, machine, source file or task. They are not recovered
vendor template identifiers.

| Internal family / legacy input alias | Native wire type ID |
| --- | --- |
| page | c1ce26cd-1045-5ca9-9a93-5a6de05de17b |
| document | d6341e22-73c7-5147-baa6-17fabad31251 |
| deck | 84dc10d0-fc8a-5c80-b8e3-383f1be4c10b |
| sheet | 7d23b0f2-07ab-54bf-8d9c-99c060584bdb |
| dashboard | 9bc33324-e201-5507-968b-ec8de241f9dc |
| design | 38bdbc54-49f4-50fb-800d-9b03665de909 |
| animation | 34ecba3f-92b4-55d7-ae3c-282d69d4dccd |
| whiteboard | 5a6d054c-bb77-5aea-b0a8-ff63780a1c63 |
| checklist | 7f8f26fa-3402-5dce-819c-c9c4077179b8 |

`frame/types[].slug`, `frame/types/{id}/create`, Make request type IDs,
source `artifact_type_id`, template `id`, and gallery/output `type_slug` now
use this mapping. Both native IDs and the old lowercase semantic aliases
remain accepted on create and Make inputs, including existing persisted
Make tasks resumed with old aliases. Alias and native-ID source comparisons
normalize to the same internal family. Page's `globe` icon matches the
original built-in page icon (`a2q.A1`); a missing icon would instead map the
catalog entry to document in `gqb`/`kmd0`.

Internal `make_type` metadata and the immutable artifact identity algorithm
continue to use semantic family names. Type-wire repair changes neither
existing artifact UUIDs, native attached-output handles, snapshot paths,
original bytes, nor the distinct-family rule introduced earlier. Native and
legacy aliases for the same family/path/bytes produce the same artifact UUID.
No destructive artifact migration is needed. Root owns actual ART parsed-row
and device-menu acceptance; local Python tests alone do not prove native
deserialization.

Make tasks use the existing model/exec/write/publish workflow. Office prompts
instruct the model to generate actual packages with the installed library
through `exec`, then call `publish_artifact`. An edit uses the original
workspace path and the correct Office library. Make preparation inherits the
selected artifact's family and rejects an explicitly incompatible type. The
quoted source includes its actual byte SHA-256, path, family, and validated
text preview when cached. Prompts require the worker to verify that byte hash
before editing; a historical snapshot whose live path differs requires the
user to select which revision to edit rather than silently replacing newer
work. This is a prompt constraint, not a filesystem compare-and-swap editor.

Publication runs the existing bounded OOXML extractor in a worker thread and
disposable child process before taking a snapshot. Existing ZIP/XML, size,
time, text-budget and external-relationship checks apply. A Make-only flag now
requires complete OPC content-type and root-relationship contracts, a main
content type matching the file extension, safe parsing of every XML part
(including unused parts), valid internal relationship targets, and successful
opening by python-docx/python-pptx/openpyxl inside the same bounded child.
Malformed content-types XML, missing root relations, format masquerading and
entity-bearing unused XML cannot gain a delivered frame. Existing ordinary
attachment extraction retains its default behavior and dependencies. Formula
strings in text extraction are shown as unexecuted formulas; extraction does
not execute macros or fetch external URLs. Malformed or over-budget packages
do not gain published snapshots.
Bytes, session owner, active tool, current reply, and channel owner are
rechecked after the parser await. Preview reads also recheck original bytes,
record/provenance identity and login validity after parsing.

A successfully validated package is an actual native `frame` output, with
the existing stable attached-output handle and byte-revision UUID. Gallery
`type_slug`, task `threadPages`, publication order, move lineage, current
revision selection, favorites, historical reads, and deletion markers reuse
the page mechanisms. Unvalidated legacy Office files remain ordinary files;
indexing them does not claim a preview. A changed indexed file loses its
cached preview status. No parser is run during startup/reconciliation.

An explicit non-page HTML family participates in the immutable publication
identity as `:make-family:{family}` after the original path and byte SHA-256.
Two tasks publishing the same path and bytes as dashboard and checklist
therefore retain separate artifact UUIDs and historical types. Republishing
the same family/path/bytes still deduplicates to the same artifact. Ordinary
page UUIDs and all other existing file identities keep their established
path-plus-SHA rule. This avoids rewriting existing page history; the earlier
experimental v2 family metadata was not deployed.

The native viewer continues to request `contentVia=frame` and
`pages/source`. The inspected original `bdc.java:85` builds a common
`_f/{slug}/_src/index.html`; `bdc.java:140–149` accepts only HTML source up to
2 MiB. This proves the shared native display contract, not equivalence of the
unknown original server templates. Native classification comes from the
catalog's icon keys: `file`, `slides`, `spreadsheet`, `chartLine`, `palette`,
`playCircle`, `shapes`, and `checklist` (`gqb`, `kmd0.k`, `a2q`).

For Office frames the server now attempts actual local visual rendering:
LibreOffice Writer/Impress/Calc → bounded PDF validation → Poppler JPEG pages.
Images are embedded as data URLs into self-contained responsive HTML, with
accessible extracted text in a disclosure. A ready visual includes the actual
rendered pages, formatting, embedded images and charts that LibreOffice can
render. Fonts, layout and pagination may differ from Microsoft Office.
The original Office package remains the downloadable source of truth. No
editable Office WebView or fake raster thumbnail is claimed.

The Linux renderer requires LibreOffice, Poppler, bubblewrap, prlimit and
fonts (deployment uses Debian packages and Noto CJK fonts). Its bubblewrap
sandbox has mandatory user/network/PID namespaces, UID/GID 65534, all
capabilities dropped, disabled nested user namespaces, cleared environment,
private HOME, a 64 MiB work tmpfs and 32 MiB temp tmpfs. Only /usr, required
system library links, /etc/fonts, /etc/libreoffice and selected runtime config
files are mounted read-only. No real user home, application workspace, /run,
/var or credential mount is exposed. The isolated renderer receives only a
read-only input package and static macro-security profile. It has no external
network and no unsandboxed fallback. The profile uses Very High macro
security plus explicit macro/runtime and OLE/DDE disabling, no trusted URL
list, and disabled Calc link updating (`Link=1`, not 0). Ordinary Calc
formulas may recalculate during visual conversion inside this sandbox; text
extraction never evaluates them.

Budgets are one renderer at a time, 20 seconds for conversion, another 20
seconds total for page images, at most 40 pages, 20 MiB PDF, 900,000 bytes of
JPEGs, and at most 1600 pixels per image edge (actual scale 1440 or 1024).
Per-process prlimit adds 1 GiB address space, CPU, file-size, 128 file-descriptor
and 32-process limits. stdout goes to a size-limited regular file; timeouts
kill the whole new process group. Up to four successful visual byte revisions
are cached in memory, not in account metadata or public storage. Native HTML
is checked against its original 2 MiB source limit. Missing renderer/isolation,
busy workers, conversion errors or exceeded budgets return an explicitly
labeled semantic-only text fallback, never a claimed visual success. Login,
original bytes and provenance are rechecked after all asynchronous parsing
and conversion before any preview is returned.

The five HTML family starters run under the existing script sandbox and
`connect-src 'none'; form-action 'none'`. Local form actions therefore use
explicit type=button handlers with native validity checks and Enter support;
they do not require enabling sandboxed form submission. Dashboard values come
only from user input. Animation starts still, has visible play/pause controls,
and stays still for reduced-motion users. Whiteboard notes support pointer
capture, keyboard movement, and single-tap direction buttons. Checklist and
whiteboard state is temporary in the current view; both expose current JSON
to copy into a follow-up Make request. A revised published HTML file becomes
a preserved version through the existing task/history workflow. Browser
storage and background task creation are not claimed.

`GET`/`HEAD /duet/workspace/artifacts/{UUID}/download` returns the immutable
published snapshot, or actual starter file for an unpublished starter. It
uses the original MIME, no-sniff, no-store, attachment filename, byte ranges,
and authenticated middleware. The preview links to this real download.
Existing attached-output reads also return the original package bytes.
Changing the live file never changes an already published snapshot.

Local validation is in `bridge/tests/test_make_formats.py`: mature-library
parsing of actual starter bytes, escaped source/native metadata, real-library
reopen/edit and formula preservation, immutable revision download/ranges,
gallery/favorites/delete/history, malformed/text overflow rejection,
publication byte-race refusal, unavailable-dependency gating, stale-preview
invalidation, and actual login logout during a blocked parser await.
`bridge/tests/test_make_render.py` separately asserts real Linux Office
rendering produces nonblank actual page images for all three formats; an
unavailable fallback cannot pass that test. Local browser checks in
`work/make-web-qa/verify.cjs` exercise the five functional HTML families at
390px and 1280px under the actual CSP. Physical Android interaction remains
unverified until the root agent's device acceptance.

Modern Web Guidance was queried offline for untrusted content and security.
I keep all extracted content as escaped text and use no dynamic HTML sink,
following `security`: "Prefer textContent or innerText over innerHTML when
setting text content." The server uses the equivalent HTML text escaping;
no Sanitizer API or DOMPurify is required because no untrusted markup is
interpreted. The Office preview endpoint uses a scriptless sandbox policy,
allowing downloads explicitly for the real original-file link.

Additional Modern Web Guidance applied: `motion` says "Provide a pause
mechanism" and "Default to static views"; animation starts paused and honors
reduced motion without hiding content. `drag` says "Use a dedicated handle"
and "Provide keyboard movement"; the whiteboard uses an actual button grip,
pointer capture, a shared clamped movement helper, and single-tap direction
buttons. `accessibility` says "Provide semantic data tables as alternatives";
the dashboard's exact entered values remain available as a real table.

Renderer configuration was checked against the exact LibreOffice 25.2.3.2
[Common schema](https://raw.githubusercontent.com/LibreOffice/core/libreoffice-25.2.3.2/officecfg/registry/schema/org/openoffice/Office/Common.xcs)
and [Calc schema](https://raw.githubusercontent.com/LibreOffice/core/libreoffice-25.2.3.2/officecfg/registry/schema/org/openoffice/Office/Calc.xcs).
Namespace and mount options follow the
[upstream bubblewrap manual](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml).

Actual Office rendering requires Linux dependencies and working namespace isolation. Validate in the deployment environment; protocol fixtures alone do not establish device/browser compatibility.
