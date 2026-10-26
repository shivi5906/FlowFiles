# FlowFiles — Project Requirements Document (PRD)

**Version:** 0.1  
**Date:** 27 October 2026  
**Status:** Initial planning  
**Platform:** Android and iOS  
**Frontend:** Flutter + Dart  
**Backend:** Python + FastAPI  
**Database:** PostgreSQL, introduced when persistent server-side data is required  
**Budget:** Zero-cost development wherever practical  
**Target initial delivery:** 14 days  
**Document owner:** shivi

---

## 1. Product Overview

FlowFiles is a mobile file-processing application intended to provide document, photo, audio, and video operations through a single interface.
Users should be able to select a file category, choose an operation, process the file, review the result, and export it in a supported format.
The product aims to reduce the need to switch between multiple applications for common file conversion, compression, editing, and annotation tasks.

## 2. Problem Statement

Mobile users may need separate tools to convert document formats, compress photos, annotate images, adjust audio, or edit videos.

FlowFiles proposes a unified mobile workflow for these operations, with consistent file selection, processing status, output preview, and export.

## 3. Target Users

Initial target audiences:

- Students handling assignments, reports, and scanned documents.
- Professionals processing documents and media on mobile devices.
- Users who need to reduce file sizes to meet upload limits.
- Users who need basic media editing and format conversion without complex desktop software.

These are initial hypotheses. Actual user research and beta feedback will determine which audience and features deserve the most investment.

## 4. Main User Workflow

The planned primary workflow is:

1. User opens FlowFiles.
2. User authenticates, where required.
3. User reaches the dashboard.
4. User selects a file category: Document, Audio, Photo, or Video.
5. User selects an input file.
6. User chooses an operation and its settings.
7. The application processes the file.
8. User reviews the output and relevant metadata.
9. User exports or shares the result.
10. FlowFiles records appropriate operation metadata for history and future product analytics.


## 5. Functional Requirements

### 5.1 Documents

Planned capabilities:

- Convert between supported document formats.
- Extract and edit document content where supported.
- Apply changes before exporting.
- Allow the user to select the desired output format.
- Provide processing status, errors, and output information.

Initial candidate formats:

- PDF
- DOCX
- TXT
- Markdown (MD)

Initial implementation priority:

1. PDF text extraction to TXT/Markdown.
2. Markdown and TXT export.
3. DOCX-to-Markdown/TXT conversion.
4. DOCX/PDF generation and conversion.
5. Editable document workflows and more complex layout preservation.

Scanned PDFs, OCR, complex tables, and high-fidelity PDF-to-DOCX conversion are advanced capabilities and require separate validation.

### 5.2 Photos

Planned capabilities:

- Convert supported image formats without intentionally changing visual content.
- Reduce file size through compression.
- Resize images.
- Add a signature, name, date, or other text.
- Export the result in the selected format.

Initial candidate formats:

- JPEG/JPG
- PNG
- WebP

Initial implementation priority:

1. JPEG input and PNG/JPEG output.
2. Image resizing.
3. JPEG quality adjustment and compression.
4. WebP support.
5. Text and signature annotation.
6. Batch processing, if time permits.

PNG transparency must be preserved when the target format supports it. Converting to JPEG requires an explicit background choice for transparent images.

### 5.3 Audio

Planned capabilities:

- Increase or decrease playback speed by generating an adjusted output file.
- Reduce file size or encoding quality where applicable.
- Convert between supported audio formats.
- Export the processed audio.

Initial candidate formats:

- MP3
- WAV
- AAC
- FLAC

Implementation recommendation:

Start with MP3 and WAV, verify codec support, and expand based on user demand.

### 5.4 Video

Planned capabilities:

- Compress videos.
- Change supported output formats.
- Trim video clips.
- Adjust playback speed.
- Remove or replace audio.
- Export the processed video without adding a FlowFiles watermark.

Initial candidate format: MP4.

Other containers and codecs should be added only after testing their compatibility.

Video processing is planned for a later implementation stage because it has higher processing, memory, storage, and execution-time requirements.

### 5.5 Authentication and User History

Planned capabilities:

- Provide a user entry and authentication flow.
- Associate persistent history with an authenticated user where applicable.
- Record operation metadata, such as input format, output format, operation type, timestamp, status, and file size.
- Support future account-based features if demand justifies them.

Do not store entire media files inside PostgreSQL. Store metadata and, if cloud storage is added later, references to the stored objects.

### 5.6 Dashboard and Export

The dashboard should expose the four main categories:

- Documents
- Photos
- Audio
- Video

Each category should lead to a file-selection and operation-selection workflow.

The result screen should display:

- Output filename and format.
- Output file size.
- Processing status.
- Errors or warnings where applicable.
- Preview where supported.
- Save, export, or share actions.

## 6. Non-Functional Requirements

### Reliability

- Never report success before verifying that the output file was generated.
- Return clear errors for unsupported, corrupted, or invalid files.
- Preserve the original file unless the user explicitly chooses to replace it.
- Test generated outputs with representative sample files.

### Performance

- Keep long-running processing away from the Flutter UI thread.
- Use background workers for server-side jobs when needed.
- Set configurable file-size and processing-time limits.
- Report progress for operations where progress information is available.

### Security and Privacy

- Validate file contents rather than trusting extensions alone.
- Use generated temporary filenames.
- Restrict upload size, file types, and processing resources.
- Avoid exposing internal server paths.
- Delete temporary files according to a defined retention policy.
- Use HTTPS for deployed APIs.
- Never log file contents or authentication secrets.
- Document whether processing occurs locally or on the server.

### Maintainability

- Keep Flutter UI separate from file-processing logic.
- Organize backend conversion services by file category.
- Use explicit API request and response schemas.
- Add automated tests for conversion logic.
- Track dependencies and review their licenses before commercial distribution.

### Cost

- Use free local development tools.
- Prefer on-device processing for suitable operations.
- Avoid paid infrastructure until there is a demonstrated requirement.
- Reassess hosting costs before introducing cloud-based video processing.

## 7. Proposed Technical Architecture

### Mobile application

- Flutter and Dart.
- Material 3 interface.
- Device file picker and share integration.
- Local preview and export.
- Local history for basic operations.
- HTTP client for backend operations.

### Backend

- Python.
- FastAPI.
- Pillow for image processing.
- PyMuPDF for PDF operations.
- Pandoc and python-docx for document conversions.
- FFmpeg for audio/video processing.
- Tesseract OCR in a later stage if required.

These tools are candidate dependencies; supported formats, licensing, platform compatibility, and output quality must be verified before release.

### Database

PostgreSQL should hold persistent entities such as users, conversion jobs, usage information, and subscription metadata if those features are implemented.

Suggested initial entities:

- `users`
- `conversion_jobs`
- `supported_operations`

A conversion job should record the operation, input/output formats, sizes, status, timestamps, and a safe output reference where applicable.

Local history can be implemented before PostgreSQL becomes necessary.

### Processing pipeline

1. Receive a file or local file reference.
2. Validate its type, size, and processing options.
3. Select the appropriate processing engine.
4. Execute the conversion.
5. Verify the generated output.
6. Record job metadata if persistence is enabled.
7. Return the result or output reference.
8. Clean up temporary data according to policy.

## 8. API Design — Initial Proposal

Suggested endpoints:

- `GET /health` — service health check.
- `GET /operations` — list supported operations.
- `POST /conversions` — submit a conversion job.
- `GET /conversions/{job_id}` — retrieve job status.
- `GET /conversions/{job_id}/result` — retrieve an authorized result.

The exact endpoints may change after the first working implementation.

For long-running video or audio operations, use asynchronous jobs instead of keeping an HTTP request open for the entire conversion.

## 9. Fourteen-Day Delivery Plan

### Days 1–3: Foundation and Image Pipeline

- Establish the repository and development environment.
- Create the Flutter application and FastAPI service.
- Implement JPEG input, conversion, validation, and output verification.
- Complete PNG/JPEG export and basic resizing.

**Milestone:** One complete, tested image-processing workflow.

### Days 4–6: Image Features and Document Extraction

- Add image compression and WebP where practical.
- Implement PDF text extraction.
- Generate TXT and Markdown outputs.
- Add clear error handling.

**Milestone:** Working image tools and initial document processing.

### Days 7–9: Document Conversion and Editing

- Add DOCX-to-Markdown/TXT.
- Implement supported document generation and export.
- Validate output quality on representative files.
- Build the result and history screens.

**Milestone:** A usable document workflow.

### Days 10–11: Integration and Stabilization

- Integrate the main Flutter and backend flows.
- Test invalid inputs and interrupted operations.
- Review privacy, temporary-file handling, and performance.
- Fix critical defects.

**Milestone:** Stable release candidate for the implemented scope.

### Days 12–14: Testing and Submission Preparation

- Test on target devices.
- Prepare store listings, screenshots, privacy disclosures, and release builds.
- Submit when the builds and account requirements are ready.
- Document deferred features and known limitations.

**Stretch scope:** Audio and video processing. Begin these earlier only if the core pipeline is stable or if one of these is the primary product differentiator.

Store approval within the same 14-day period is not guaranteed.

## 10. Acceptance Criteria

The MVP is considered functionally ready when:

- A user can select a supported input file.
- The app presents valid operation choices.
- A supported operation produces a valid output.
- The app does not report success for a failed conversion.
- The output can be previewed where supported and exported.
- The original file remains unchanged by default.
- Invalid files receive understandable error messages.
- Processing does not freeze the mobile interface.
- File-size and format limitations are enforced.
- Automated tests cover the implemented conversion paths.
- The release build has been tested on the target platform.

## 11. Risks and Limitations

- Document layouts may change during PDF/DOCX conversion.
- Scanned documents may require OCR.
- Compression can reduce quality.
- Not every format or codec is supported by every library build.
- Large media files can exceed free hosting limits.
- Background processing and mobile file permissions differ between Android and iOS.
- Store review and account requirements can affect the delivery timeline.
- A zero-budget hosting setup may not support production-scale video processing.

## 12. Future Monetization

Potential future options:

- Free basic conversion.
- Paid advanced processing or batch operations.
- Premium OCR or document tools.
- Higher processing limits.
- Optional cloud history and cross-device functionality.

Monetization should follow user feedback and validated demand rather than delaying the initial working product.

## 13. Definition of Done

A feature is complete only when:

1. The implementation is integrated.
2. Expected behavior is documented.
3. Relevant tests pass.
4. Errors and limitations are handled.
5. The output is verified using real files.
6. Changes are committed to Git.
7. Documentation reflects the actual implementation.

## 14. Immediate Next Steps

1. Freeze the initial image-processing scope.
2. Create the GitHub repository and project structure.
3. Build the JPEG-to-PNG/JPEG backend pipeline.
4. Write tests using valid, corrupt, and unsupported input files.
5. Integrate the first conversion flow into Flutter.
6. Review the first working version before adding document processing.

---

**Product principle:** Build a small number of dependable file operations first. Expand the format matrix only when the current operations are tested, reliable, and useful to real users.