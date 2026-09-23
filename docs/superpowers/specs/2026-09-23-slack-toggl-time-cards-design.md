# Slack Toggl Time Cards Design

## Goal

Record research, university, and work time from fixed Slack bot cards without opening Toggl.  Kei Agent starts one local timer at a time, writes a completed entry to Toggl when the user stops it, and records the resulting research or university activity in its respective Notion home.

## Scope and decisions

- The primary control is one Kei Agent control message in each enabled `10_`, `20_`, and `30_` channel.  It uses fixed labels rather than channel-specific button labels.  The user pins it in Slack once; Slack bot tokens cannot manage message pins.
- Idle cards show `開始`.  Running cards show `停止` and `メモを追加`.  The `20_course` card instead shows `授業を選んで開始`.
- A user has exactly one active timer.  Starting another timer finalizes the previous timer at that instant before starting the next one.
- The running interval is owned by Kei Agent's SQLite store.  Toggl receives one completed entry only at stop time; Toggl will not show a live running timer.
- Descriptions are derived from the domain and current channel/course: `研究 / <channel>`, `大学 / <course>`, or `仕事 / <channel>`.  A memo is optional and appended only when the user adds one.
- Work time is not written to Notion.  Research and university time are both written to Notion after Toggl succeeds.
- Channel names stay short.  In particular, a course channel such as `20_001_mma` is associated with a course by Slack channel ID, not by parsing the display name.

## User interaction

### Card lifecycle

On first setup, Kei Agent posts a card in each enabled channel and asks the user to pin it once in Slack.  The store retains the channel ID and Slack message timestamp.  Later changes use `chat.update` on that message rather than creating a stream of control messages.  If the message was deleted, the next setup or button interaction posts a replacement; the user pins that replacement if needed.

The card states are:

| State | Controls | Text |
| --- | --- | --- |
| Idle | `開始` | No active timer in this channel |
| Running here | `停止`, `メモを追加` | Started time and the inferred description |
| Running elsewhere | `開始` | Names the active channel and explains that starting switches timers |
| Sync attention | `再送` where applicable | Explains which destination needs attention without exposing secrets |

Slack actions are acknowledged before any network operation.  Opening a memo or course picker uses a Slack modal; the final start/stop operation runs after acknowledgement.  Buttons are accepted only from `KEI_AGENT_ALLOWED_USER_ID`.

### Course selection and routing

For `20_course`, the course picker requests only records whose `状態` is blank or `履修中` and whose `学期` contains the current date.  This reuses the existing course-agent term filtering, so past courses never appear as candidates.

For a mapped individual course channel, the course is selected without a picker.  A `course_channel_bindings` row maps the Slack channel ID to the canonical `授業` page ID and label.  Changing `20_001_mma` to another short display name does not alter that association.  Unmapped course channels remain usable as a general university timer, but do not attach a course relation until the user maps them.

Research and work use the Slack channel's current name as their label.  A top-level `20_course` entry uses the selected canonical course label.

## Components and boundaries

### Main Slack service

A dedicated `time_cards` module owns Slack card blocks, button/modal parsing, and the local timer state machine.  `Assistant` delegates action bodies to it; `app.py` registers only the `kei_agent_time_*` action patterns.  The module receives narrow interfaces for Slack posting/updating, the Store, Toggl writing, and destination sync.  It does not access a Notion token.

### SQLite store

New tables retain only operational state:

- `time_cards`: card channel ID and message timestamp.
- `course_channel_bindings`: Slack channel ID, canonical course page ID, and course name cache.
- `time_entries`: a generated entry ID, owner Slack user ID, domain, channel information, optional course information, start/end timestamps, description, memo, and independent Toggl/Notion sync states.
- `active_timers`: one row per Slack user ID pointing at an unfinished `time_entries` row.

The start operation and replacement of an active timer are SQLite transactions.  A process restart leaves the active row intact, allowing the card to recover as running.  Finished rows are immutable apart from memo and delivery-state fields.

### Toggl writer

`kei_agent.timelog.Toggl` gains a narrow authenticated write operation for one completed Focus time entry.  The existing read/report API remains compatible.  The main service alone receives `TOGGL_*` environment variables; agents remain stripped of those credentials.

Completed entries use the existing human-time project convention:

- Research: `研究 / <channel>`
- University: `大学 / <canonical course>`
- Work: `仕事 / <channel>`

Course reporting normalizes the university prefix before matching the canonical course name, so existing course totals keep working.  Project identifiers are cached only after a successful discovery/setup operation; no per-card polling is performed.

Toggl receives no request on start and one write request on normal stop.  This avoids consuming the Focus Free request quota for elapsed-time displays.  The card computes elapsed time from its stored start timestamp.

### Notion destination writers

The Slack main process never receives the university Notion token.

- The course A2A service gains a small, deterministic `record_study_time` skill.  It creates or verifies a `学習ログ` database under the authorized university home, then writes a row with `日付`, `時間（分）`, `科目` relation when known, `メモ`, `Slack`, and `Kei Agent 記録ID`.
- The research Notion Gateway gains a similarly narrow time-log operation.  It creates or verifies `研究ログ` under the research home, then writes `日付`, `時間（分）`, `テーマ`, `メモ`, `Slack`, and `Kei Agent 記録ID`.

Both writers find an existing row by `Kei Agent 記録ID` before creating one.  This makes Notion delivery safely retryable and stays inside the authorization boundaries already established for the course home and research home.

## Stop and synchronization flow

1. A stop action atomically stamps the active row with `ended_at`, clears the user's active timer, and updates the two affected cards.
2. The main service posts the completed entry to Toggl.  On a confirmed success it sets `toggl_state=done`.
3. For research or university rows, it calls only the domain's destination writer.  On confirmed success it sets `notion_state=done`.
4. The Slack card and a short thread reply report the completion.  They say `Notionへの記録はあとで再送する` only if that destination is pending.

Notion errors are retried from persistent pending rows at service startup and on subsequent card actions.  A Notion request is idempotent by record ID.

An ambiguous Toggl outcome (for example, a connection ending before a response) is marked `needs_review`, not automatically retried: the original request may already have created an entry.  The card offers `再送` after the user has chosen to retry, avoiding silent duplicate time entries.  A definite validation or authentication error is reported clearly and leaves the local record intact.

## Slack setup and permissions

Slack bot tokens cannot add, remove, or inspect message pins.  The setup command posts each control card and tells the user to pin it manually; after that, all card state changes remain in the same message.  Existing message posting/updating and modal capabilities are reused.

The Slack app manifest requests only the channel-discovery scopes needed for the channel types in use (`channels:read` for public channels and `groups:read` for private channels).  Setup validates these scopes before discovery, reports channels that the bot cannot access, and never claims a card is pinned.  A setup command installed with the service discovers only enabled target channels and creates/replaces their cards.  It does not use course display names as a source of academic truth.  The command is safe to run repeatedly.

## Failure handling and privacy

- A Slack action is acknowledged within Slack's three-second limit.  All external work happens afterward and is logged without tokens or raw Notion content.
- A deleted/archived channel is skipped and its card binding is retained only as inactive history.
- A running timer survives process restart.  The next interaction can stop it; no timer is invented after an unclean shutdown.
- All generated Slack messages use the existing soft Japanese voice; no implementation status narration precedes the result.
- Academic record data never appears in the timer description beyond the selected course name.  Existing user-owned grades, academic databases, and documentation are not changed by this feature.

## Verification

Automated tests cover the timer state machine, single-active-timer replacement, course binding lookup, card rendering, unauthorized button presses, modal validation, Slack acknowledgement order, Toggl write payloads, ambiguous-write handling, Notion idempotent retry, and restoring an active timer after reopening the SQLite database.

Integration checks use fake Slack and fake Toggl/Notion transports.  Deployment verification confirms the assistant, course service, and research gateway are reachable; real credentials are never printed.  A manual smoke test confirms an idle card appears, the user can pin it, an entry can be started/stopped, and an optional memo reaches the expected destination.
