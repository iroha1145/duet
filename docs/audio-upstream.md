# Audio provider protocols

The text chat model and cache settings are independent of these audio settings.
Both audio services keep their own server-side API key; omitted `api_key`
preserves the existing key, and an empty string clears it. No native Android
protocol, playback speed range, FIFO credits, or owner lifecycle is changed.

## Transcription

`PUT /duet/speech/config` accepts `language_format`:

| Value | Multipart language hint | Intended provider |
| --- | --- | --- |
| `legacy` (default) | `language=zh` | Existing compatible transcription services |
| `languages` | `languages[]=zh` | Current OpenAI `gpt-transcribe` |

Automatic language detection omits both fields. The native pinned language is
still reduced to its base language code. The services receive the same bounded
WAV file and `response_format=json`; only the language hint changes. The bridge
reads the returned `text`. There is no retry with another request shape/model.

## Read aloud

`PUT /duet/tts/config` accepts `transport`:

| Value | Upstream workflow |
| --- | --- |
| `speech_http` (default) | `POST {base_url}/audio/speech`, headerless mono PCM16 at 24 kHz |
| `realtime_ws` | One private `{base_url}/realtime?model=...` WebSocket per synthesis |

An existing HTTP(S) API base such as `https://api.openai.com/v1` is used for both
modes; the Realtime adapter derives its WS(S) scheme. Models beginning with
`gpt-realtime` are rejected when paired with `speech_http`. No automatic model
selection or fallback occurs. For the current official workflow, explicitly set
`model=gpt-realtime-2.1-mini`, `transport=realtime_ws`, and a supported voice such
as `marin` or `cedar`. Existing custom voice catalogs continue mapping native
voice IDs to provider voice strings. Changing transport invalidates preview
cache URLs through the existing nonsecret configuration revision.

The Realtime adapter authenticates with the server-held bearer key. Redirects
are rejected before forwarding credentials. It checks `session.created` and the
effective `session.updated` model, voice, audio-only modality, PCM24k format,
speed 1, disabled input turn detection, and absence of tools. A single isolated
`response.create` carries only the literal text and a read-aloud instruction.
It binds the response ID, item ID, output/content indices, and request metadata;
only `response.output_audio.delta` bytes reach the existing converter.

A successful response requires nonempty even-byte PCM, audio-done and transcript
evidence, and a matching `response.done` with `status=completed` and exactly one
completed assistant audio item. Final transcript text must agree across events
and with the source after Unicode NFKC normalization and whitespace folding.
Words, numbers, punctuation, and case are not rewritten or compared semantically.
Mismatch fails with `upstream_text_mismatch`; malformed, truncated, incomplete,
cancelled, or provider-error responses never produce `SpeechComplete`. The
bounded HTTP WAV preview returns 502 JSON rather than a partial successful WAV.

Realtime is generative: these checks do **not** guarantee verbatim speech or
acoustic correspondence between audio and transcript. Streaming audio already
played cannot be retracted when a late mismatch is detected. Natural changes to
punctuation or spoken numbers can trigger conservative rejection. No paid
correction, retry, or alternate-model call is made. The server's existing global
deadline, PCM limit, tempo/frame pacing, and cancellation still apply; additional
per-receive deadlines bound a silent Realtime provider.

On late source failure, converter cleanup cancels and joins the normal readers,
kills the owned ffmpeg process, discards its remaining stdout in bounded chunks,
and then waits for exit. Waiting before draining can hang on Linux even after
SIGKILL if stdout is paused at its high-water mark. This cleanup does not alter
the normal FIFO credit, input lookahead, speed, or playback frame calculations.

## Verification and sources

`tests/test_audio_upstream.py` uses only local HTTP/WS fixtures. It covers the
multipart shapes, migration/isolation, session/request wire format, redirects,
audio/transcript/completion errors, limits, cancellation, actual ffmpeg/Opus
playback integration, and preview failure behavior. It does not prove model
availability, live pronunciation, or physical Android playback.

Official contracts checked 2026-11-15:

- [File transcription](https://developers.openai.com/api/docs/guides/speech-to-text.md)
- [Text-to-speech migration notice](https://developers.openai.com/api/docs/guides/text-to-speech.md)
- [Realtime WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets.md?api=realtime)
- [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events.md)
- [Realtime server events](https://developers.openai.com/api/reference/resources/realtime/server-events.md)

## Realtime read-aloud event handling

The adapter uses one private WebSocket for one response, with inline literal
text, no microphone input or tools, and `conversation: "none"`. This setting
keeps response output out of default history; it is not a promise that item
lifecycle notifications will be absent.

The adapter handles this terminal sequence:

```text
response.output_audio.done
response.output_audio_transcript.done
response.content_part.done              part.type = audio
conversation.item.done                 no response_id
response.output_item.done
response.done                          completed, matching request metadata
```

`conversation.item.done` is an item-level notification. It is correlated by its
`item.id`, not by a nonexistent required `response_id`. The adapter accepts only
the same assistant message item, completed with one `output_audio` transcript.
Content must agree with the transcript terminal, other completed-item events,
and the final response. This notice cannot authorize audio or replace a matching
completed `response.done`. Missing response completion, mismatched IDs/content,
provider errors, interrupted responses, or unknown event types fail closed.

The official lifecycle also includes `conversation.item.added` and the documented
`conversation.item.created` event. Only an empty in-progress assistant message is
accepted. If such a notice arrives before a response-scoped item event, its ID
is a bounded candidate; the later event carrying this response's `response_id`
must bind the same ID before PCM is accepted. There is at most one candidate
item, and at most one notice of each of these three item event types. No user,
tool, deleted, truncated, or input-audio events are accepted on this socket.

`conversation.created` is a separate session-initialization notification. At
most one correctly shaped resource with a nonempty ID and the
`realtime.conversation` object type is accepted after `session.created`.
`rate_limits.updated` may interleave with response creation or output. Its
bounded list of objects is informational and never affects binding or success.
These are explicit allowed event types; other notifications are not skipped.

Bounds per synthesis: 32,768 received JSON events, 64 informational notifications,
16 entries per rate-limit notification, 512 characters per candidate/previous
item ID, and 32,768 characters per transcript string. The pending-notice state
stores only one ID, at most three event-type names, and one terminal transcript
(at most 128 KiB UTF-8). The existing transcript delta/final buffers and 512 KiB
WebSocket message limit remain bounded separately. No audio queue was added;
PCM still enters the existing paced converter in chunks of at most 8 KiB.

Realtime remains generative. NFKC/whitespace-normalized transcript agreement does
not guarantee acoustic verbatim reading; late failure cannot retract streamed
audio. No retry, paid fallback, alternate model, or text rewriting is performed.

`bridge/tests/test_audio_upstream.py` exercises the observed sequence, optional
initialization/item notifications and both terminal orders, plus wrong IDs,
roles, content, status, unknown events, limits, missing response completion,
preview failure, cancellation and actual ffmpeg/Opus integration. Those are
local fixtures, not evidence of a successful new live-provider run.

Official references checked 2026-11-15:

- [Server events](https://developers.openai.com/api/reference/resources/realtime/server-events.md):
  `conversation.item.done`, `conversation.item.added`, `conversation.item.created`,
  `conversation.created`, `rate_limits.updated`, and response lifecycle schemas.
- [Client events](https://developers.openai.com/api/reference/resources/realtime/client-events.md#response.create):
  out-of-band response configuration.
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations.md#handling-audio-with-websockets):
  lifecycle events and concurrent deltas.
