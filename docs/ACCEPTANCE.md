# Acceptance case: October 15, 2026 Life Science Virtual PitchForce

The original files are private and are **not** in this repository. The expectations below come from an earlier manual review. The automated tests check the same behaviors on a synthetic equivalent; `pitchforce/sample_data.py` uses fictional names.

## How to run it on the real files
1. Put the originals in a git-ignored folder (`private/`). Create an event dated 2026-10-15, timezone America/Los_Angeles, format virtual, 6 expected judges, webinar ID `88406470439`.
2. Upload:
   - the roster(s)
   - the wall-clock TXT transcript and the recording-relative VTT
   - the MP4 recording
   - `report_Follow-Up- Presenting Hemavive(1).csv`
3. Run processing, then check the items below.

| Expectation | Where it is checked | Synthetic test |
|---|---|---|
| Hemavive pitch about 16:08:39–16:14:09; questions 16:14:24–16:18:25; feedback 16:18:34–16:20:38 | Company → Transcript tab / boundaries | `test_hemaflow_segments_questions_feedback` |
| Clip about 01:08:25.6 → 01:20:24.0 (about 11 min 58 s), not padded | Video clip tab | `test_sample_clip_end_to_end_and_stale_after_boundary_change` |
| TXT starts 14:20:33 and the recording starts about 15:00:14: the first transcript time is never treated as video 0:00 | Alignment details | `test_alignment_offset_never_uses_first_timestamp` |
| Announced 21/30 shown with attribution; not recalculated; no invented component scores | Score tab and report | `test_sample_processing_results` |
| Five identifiable panelists. The host said "Mo/Moe" could not attend: shown as a proposal only, not a confirmed identity | Review queue (absence item) | `test_absence_mentioned_by_host_is_only_a_proposal` |
| Feedback themes: timeline vs spending; shorter answers; IP/competitive protection; investor return and liquidity; dense slides | Feedback tab, priorities | `test_hemaflow_segments_questions_feedback` |
| Poll CSV: Overview, Launch History and Poll Details sections; 18 launches; 168 rows; multiline multi-select answers; trailing empty columns | Sources → summary | `test_zoom_multisection_csv_with_quoted_newlines_and_trailing_columns` |
| Only the 2026-10-15 16:18:31 launch counts: responses 49–53; 1 "need more info", 4 "not interested", 0 invest, 0 free resource; the other 163 rows excluded | Interest tab, workbook | `test_event_filtering_excludes_historical_and_counts_people_once` |
| The information request is classified as an information request, not investment interest | Workbook "Lead type", report wording | `test_company_package_contents_and_isolation` |
| Response 50: name and email disagree with the roster → flagged, not merged | Review queue (contact) | `test_anonymous_and_conflicting_contacts` |
| Historical rows with names resembling panelists do not become judge contacts | Only event-specific rows are matched | `test_event_filtering_excludes_historical_and_counts_people_once` |
| Transcript "George Wang / CaptionLens" vs roster "Cash McFarlane / Birdilens": the operator confirms the association; both spellings (McFarlane in the roster, McFarland in the operator's note) are preserved | Review queue → confirm; Companies & people | `test_alias_case_requires_review_and_preserves_spelling` |
| "Jeff Missling / Anumio" vs roster "Frenelle Pharma" (transcribed "Fresnel Pharma"): a related-entity question, never merged automatically | Review queue (related companies) | `test_related_entities_not_merged` |

**Not verified:** none of these expectations has been checked against the real October 15 files, because they were not available in the build environment.
