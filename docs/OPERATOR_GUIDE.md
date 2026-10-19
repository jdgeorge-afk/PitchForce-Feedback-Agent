# Operator guide

This guide is for the person running a PitchForce event. You do not need any technical knowledge.

## 1. Create the event
**Events → New event.** Enter the name, date, timezone (Pacific by default) and format.

Under *Judging*, set how many judges you expect (usually 6) and the missing-judge rule. The standard rule applies only when exactly one judge is **confirmed** absent.

Optional:
- The Zoom webinar ID, which helps match poll results to this event.
- The time the recording started.
- A Zoom replay link. This is stored for reference only; it is not used as a source file.

## 2. Upload materials
On **Sources**, choose what kind of file it is, then drag the file in. Large recordings upload in pieces. If the connection drops, select the same file again and the upload resumes where it stopped.

| Upload | Notes |
|---|---|
| Event recording (MP4/MOV/MKV/WEBM) | Needed for clips |
| Transcript (TXT, VTT or SRT) | Upload both Zoom's meeting transcript and the recording's VTT if you have them; the app uses them to line up times |
| Company / presenter roster | XLSX or CSV, or add companies by hand under *Companies & people* |
| Judge roster | Names, firms, emails, attendance |
| Judge scores | XLSX/CSV, a Zoom chat export, or typed in by hand |
| Investment-interest poll exports | Zoom CSV or workbook |
| Optional | Decks, fact sheets, event guidelines |

The **What you have and what is missing** panel explains what can still be produced. For example, without a video the clips are marked unavailable; without a poll export, interest is marked "missing", not zero.

## 3. Check companies and people
On **Companies & people** you can:
- Rename a company or person. The old name is kept as an alias.
- Add other names.
- Record how a name heard in the pitch relates to the roster company (product, parent, licensor…).
- Set judge attendance and correct who each transcript speaker is.

A correction applies to this event only, unless you choose *All events*.

## 4. Run processing
On the **Overview**, press **Run processing**. You can leave the page; progress continues and is saved. Each company is processed on its own, so one problem does not stop the others. The **Activity** page shows every task, lets you retry failures or cancel running work, and shows any AI or transcription cost estimate.

## 5. Resolve the review queue
The **Review queue** lists what the app could not decide by itself, with the evidence, for example:
- "Is this presenter the roster company?"
- "Which company is this poll about?"
- "The host said a judge couldn't attend — confirm?"
- "This response names one person but uses another person's email."

Choose an answer. Your decision is recorded with your name, and processing re-runs where needed.

## 6. Review each company
Open a company from **Results & reports**:

| Tab | What you can do |
|---|---|
| **Report** | Read the draft (about 650–900 words). Edit any section, approve it, or regenerate it. Your edits are never overwritten; if the evidence changes later, the section is flagged *needs review* instead. |
| **Questions / Feedback** | See every extracted question and panel comment with the original words. Fix paraphrases, or remove items that are not real questions. Moderator questions are labeled separately. Gaps in the recording are highlighted and never filled in. |
| **Score** | See the full calculation and any difference from the announced total. |
| **Interest** | See each poll response exactly as submitted, and track outreach status. |
| **Video clip** | Play the recording, check where the pitch starts and the feedback ends (click transcript lines to jump), adjust the start and end, then **Render clip**. Check the automatic results (length, audio/video present, opening and closing, next company not included), then mark the clip reviewed. You can create an expiring share link if the app is reachable from the internet. |
| **Transcript** | Move lines to a different company, or correct a speaker. |

## 7. Download
On **Downloads**, build a package for one company or for the whole event. Founder packages contain only that company's files. The event package adds an organizer folder (audit workbook, issues summary, data export), which you should not forward to founders.

Files marked **out of date** were made before a change; rebuild them. Email drafts are text for you to copy; the app never sends email.

## Settings
- **Report wording & theme:** colors, brand text, the advisory invitation on/off, and the rules that sort poll answers into categories.
- **Best practices:** the EcoVG guidance used in reports. Edit it, or upload the original cheat sheets; the app flags contradictory wording and ignores archived copies.
- **Services & AI:** which analysis and transcription providers are configured, and what data they receive.
- **Calendar sources:** refresh the official EcoVG calendar. Sessions are recommended only if their date is verified and still upcoming.
- **Users:** add colleagues and give them access to events.

## Sample mode
**Load sample event** creates a fully synthetic event so you can try everything. Every page and file is marked SAMPLE.
