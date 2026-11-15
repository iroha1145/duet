# Native attachments and file previews

The Android UI, attachment IDs, organization checks and immutable output provenance remain the existing native flow. Uploads keep the exact original bytes. Parsed text is additional, bounded model input; it does not replace the downloadable file.

## Accepted input and explicit limits

- PNG/JPEG/GIF/WebP keep the existing real-image validation and 6 MiB cap.
- PDF, DOCX, XLSX, PPTX and UTF-8 text use a 20 MiB original-file cap. The native project response advertises 10 files per message and 20 MiB per file; the server also enforces 20 MiB in aggregate.
- Each extraction permits at most 200,000 characters and 256 KiB of UTF-8 text. Combined extracted attachments are limited to 256 KiB before a message is accepted. This is the adapter's attachment budget, not a guarantee that every configured upstream model has that much free context after conversation history.
- Original files exceeding an extraction budget remain downloadable. Their `extraction.status` is `too_large` or `unavailable`, with a diagnostic code and limits; sending them fails explicitly. Partial extracted text is never supplied as if complete.
- Encrypted/damaged PDFs, unsafe XML/ZIP paths, executable or macro-bearing Office packages and invalid UTF-8 return explicit errors. Image-only PDFs can preview, but cannot be sent as extracted text without OCR; this adapter does not perform OCR.
- Unsupported legacy binary Office, macro-enabled formats, archives, media and additional image formats are refused rather than relabeled as supported documents.

## Extraction and execution boundaries

Production dependencies are pinned in the server requirements to `pypdf==7.1.0` and `defusedxml==0.7.1`. OOXML is read with Python's ZIP reader and defused XML; no Office process or document converter is launched. Links are never fetched. Formulas are included as literal text with their saved cached values; they are not recalculated. Macros and embedded executables are not executed. PDF JavaScript/actions are not executed, and the PDF library's optional external JBIG2 decoder is disabled.

PDF extraction includes ordered page text and the actual page count. DOCX includes body/table paragraph text and available header/footer/footnote/endnote text. XLSX includes sheet names, cell coordinates, saved values and literal formulas; styles, date/number formatting, charts and embedded images are not interpreted. PPTX follows presentation slide order and includes available speaker-note text. Formatting, images, charts and visual layout are not reproduced in model text; extraction metadata records these limitations.

Each parser runs in a disposable child process with a 512 MiB address/process-memory cap, 6 seconds of CPU time and a 10-second wall-clock timeout. Linux uses resource limits; Windows uses a Job Object. At most two extraction children and four uploads run concurrently. ZIP packages allow at most 1,024 members, 8 MiB per expanded member, 32 MiB total expanded size and a 200:1 compression ratio. PDF parsing also limits decoded streams, page-tree depth, objects and form invocations, and permits at most 200 pages. Limit failures never silently truncate model input.

Uploads await extraction in a worker thread, then recheck login validity before storing anything. PDF publication and reading await parsing without blocking the event loop. File hashes/revisions, publication owner and current read authorization are rechecked after the await. Snapshot, provenance, task and message writes remain on the event loop. Startup reconciliation and wire projection only read cached PDF metadata and never start parsing. Validating an old PDF updates its existing root/reply projections through the existing channel update events.

Uploaded-file downloads only verify raw bytes and their persisted digest; a missing or outdated extraction cache never starts a parser. Sending such an attachment migrates its cache asynchronously using bytes-only worker input, then rereads every attachment's current record and digest before event-loop cache commits. Live request authorization, channel ownership/archive state and duplicate client IDs are rechecked after parser awaits. These request checks do not bind an already accepted background task to a device login's later lifetime.

## Download and range protocol

Native uploaded content remains under `/api/organizations/{organization}/files/{uuid}/contents`. Responses include the real MIME, a content-revision ETag, `Accept-Ranges: bytes`, and RFC 5987 `filename*` preserving Unicode and literal percent characters. Single HTTP byte ranges support 206, suffix/open-ended ranges, 416 and strong `If-Range`; a stale `If-Range` returns the full representation. Organization, authentication and digest checks precede range slicing.

`POST /v1/code/channels/{channel_id}/files:open` accepts optional nonnegative integer `range_offset` and positive integer `range_length`. Explicit ranges return base64 raw bytes even when a range splits a UTF-8 character. `size_bytes` is the returned byte count; `total_size_bytes`, `range_offset` and `range_length` describe the slice, while `content_revision` identifies the entire file. Offset exactly at EOF returns an empty slice; an offset beyond EOF is 416.

PDF output metadata advertises `has_preview` and `page_count` only after successful validation of the real immutable bytes. `rendition=display` uses the original PDF bytes and actual page count. Invalid PDFs remain downloadable without claiming a preview. No synthetic PDF thumbnail is returned.

## Verification and sources

Fixtures under `tests/fixtures/attachments/` contain no account data. They were generated with ReportLab 5.1.0, python-docx 1.2.0, openpyxl 3.1.5 and python-pptx 1.0.2; those generators are test-only, not server dependencies. Tests verify actual page/paragraph/table/cell/formula/slide content, both provider request formats, storage reopening, unmodified downloads, malicious packages, parser limits, range behavior and asynchronous ownership/integrity checks. The existing image tests remain applicable.

- [pypdf extraction and its limitations](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)
- [Pinned pypdf configuration source](https://github.com/py-pdf/pypdf/blob/7.1.0/pypdf/_configuration.py)
- [defusedxml security model](https://pypi.org/project/defusedxml/)
- [Python ZIP reader](https://docs.python.org/3/library/zipfile.html)
- [Windows Job Object limit configuration](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject)

The original Android wire evidence is in the retained decompile: `crl` multipart upload, `aw00` blob response, `e7v/a2s/b2s` attachment rules, `ijd0.m` content URL, `q820` file ranges, `an2/fam/w9q` attached-output renditions and `ez30.s` decoded content. Android display/save behavior and Linux `openat` protections still require the main task's platform validation; local fixtures do not establish those results.
