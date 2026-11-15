# Native voice preferences and sample previews

The server bundle includes support for the original Android AccountSettings and SpeechInputConfig wire formats. Protocol and real Linux codec checks use isolated provider fixtures. They do not establish the quality or compatibility of an owner's live speech provider; the available catalog is hidden until that provider is configured.

## Owner configuration

The existing authenticated `PUT /duet/tts/config` accepts its existing provider settings plus `voice_catalog` and `default_language_code`. For example (all provider names are placeholders):

```json
{
  "enabled": true,
  "base_url": "https://owner-provider.example/v1",
  "model": "owner-selected-tts-model",
  "api_key": "owner-provider-key",
  "voice": "",
  "default_language_code": "en",
  "voice_catalog": [
    {
      "code": "en",
      "display_name": "English",
      "voices": [
        {"id": "owner_a", "display_name": "My configured voice A", "provider_voice": "actual-provider-id-a"},
        {"id": "owner_b", "display_name": "My configured voice B", "provider_voice": "actual-provider-id-b"}
      ]
    }
  ]
}
```

Use truthful owner-selected display names. These voices do not claim to reproduce the original application's factory voices. Native IDs must be unique across the catalog, lowercase ASCII letters/digits/underscore/hyphen, 1–64 characters, and cannot be `mellow` (rejected by the native validator). Catalogs allow at most 8 languages, 8 voices per language, and 16 voices total. Language codes use 2–3 lowercase letters and an optional region/script segment; configured default must exist. Labels are bounded to 80 characters. Provider voice IDs are strictly owner-configured and never accepted from client selections. The complete merged UTF-8 TTS configuration, including preserved credentials, must fit the existing 16 KiB reader limit; oversized deltas are rejected before atomic replacement, leaving the previous configuration readable and editable.

Existing singleton `voice` configurations migrate compatibly: native ID `airy` maps to that configured provider voice, displayed as `Configured voice`, under the configured default language (`en` by default). Once a nonempty explicit catalog exists, `airy` is rejected unless explicitly included. Unknown or removed IDs fail; no switch to a global provider voice occurs. An omitted TTS query voice selects the owner's catalog default. Existing playback speed control still uses the existing real atempo conversion; upstream speed remains 1.

## Native persistence and bootstrap

`PATCH /account/settings` (also `/api/account/settings`) accepts a nonempty pure delta containing only:

- `voice_preference`: current configured native voice ID;
- `voice_speed`: numeric 0.8, 1, 1.2, or 1.5 (booleans rejected);
- `voice_language_code`: configured language code;
- `dictation_language_code`: configured language code or `auto`.

The original client performs optimistic local persistence and usually ignores the voice PATCH response. Values therefore persist atomically in the private server-owned `voice-preferences.json`, and appear on `GET /account`, bootstrap, and organization app_start. They belong to this bridge's single synthetic account, shared across its devices, rather than a device. There is no claim of immediate push to another already-running client; the next account/bootstrap refresh observes them. Pure MCP settings retain their existing permission epoch, storage and ETag behavior. Mixed MCP/voice bodies and unsupported fields, including `personalized_drafts`, are rejected before either store writes. MCP bodies retain the original bounded 1 MiB JSON upload capacity and 2049 boolean-leaf contract; pure voice deltas have an additional 8 KiB limit. Both paths recheck account binding and the exact authenticated access-token epoch after body upload and immediately before the synchronous commit, so logout, revocation and access refresh during upload cannot authorize a late write.

The native feature projection supplies exact `speech_input.supported_languages[].{code,display_name,voices:[{id,display_name,preview_url}]}`, `default_language_code`, and `cai_gleaming_goblet` only when TTS is actually ready. Provider IDs and credentials are absent. Failed/unconfigured TTS hides the available catalog. Populating preferences does not enable Talk: its existing ASR, TTS, model and runtime checks remain in force. Language preferences are persisted; actual provider language ability depends on the owner-selected provider/voice. Separate dictation language remains a separate native field.

## Preview contract

The native player fetches a same-origin absolute URL using authenticated native OkHttp, then caches it privately by URL. `GET /duet/voice-previews/{revision}/{voice}.wav` (also `/api/...`) synthesizes only the server-owned nonprivate sample `This is a sample of the configured voice.` on an explicit preview GET. Bootstrap and catalog requests never generate audio or make provider calls. There is no arbitrary query text, source URL, proxy, or paid automatic pre-generation.

The URL includes a digest of catalog/provider/model configuration so native caches invalidate after those changes. Credentials are excluded from the public digest. Returned WAV is mono 16-bit PCM at 16 kHz, using the existing TTS PCM/ffmpeg conversion and private HTTP session. Audio is kept only in bounded memory (15 seconds max); provider PCM, timeout, content validation and cleanup reuse the existing TTS limits. Preview and read-aloud share the four-active-operation bound. Query parameters, stale revisions and unknown IDs fail. Requests recheck active authorization and a private complete configuration snapshot while waiting, including a silent provider. Credential-only changes cancel in-flight work while leaving the public URL digest independent of credentials; disconnect/revocation cancels and reclaims work. Responses are private/no-store and no text/audio/provider key is logged or saved by these handlers.

Preview requires the existing Linux runtime dependencies: ffmpeg with azmq/atempo, pyzmq, and libopus0 for advertised native read-aloud capability. Existing Windows codec skips do not establish Linux behavior. The targeted fixture tests additionally exercise actual local ffmpeg PCM conversion and WAV playback structure without paid provider calls.

## Source evidence and scope

Authoritative static wire evidence is retained in `work/voice-preferences-readonly-checkpoint.md`: original `na/oa`, `gs80/nc90/pc90`, native `t4` TTS query, `xza/hgl` preview fetch/cache, and `bb/db/n7` settings delta/bootstrap behavior. Read replies aloud is the native local DataStore toggle; Android one-press assistant setup and actual writing-style learning remain outside this server change. Talk's protocol and speech/dictation conversion were not rewritten.
