# Engineering report — production-readiness pass

**Date:** 2026-11-13 · **Branch:** `chore/production-readiness` · **Base:** `Main` @ `b780c59`

## 1. Verdict

**Not Production Ready** for a public launch with live money. **Ready for a
staging / demo deployment** on Paystack test keys.

The payment core is sound and is now tighter, and every defect fixed here is
locked by a test. The verdict is held back by the open items in §5 — chiefly
durable storage for payment proofs, a shared rate-limit store, and the fact
that nothing has yet been exercised against the real Paystack API.

## 2. What the system is

A modular Django 5.2 + DRF monolith (`backend/`) with a React 18 + Vite SPA
(`frontend/`). Departments publish fees; students pay through Paystack
(direct collection — the app never holds funds) or submit proof of an offline
payment for a class rep or admin to approve. Token auth, three roles
(student, class rep scoped to department + level, admin). SQLite locally,
PostgreSQL when `DATABASE_URL` is set. Render blueprint + Vercel config exist.

No technology was added or replaced in this pass.

## 3. Baseline and result

| Check | Before | After |
|---|---|---|
| Backend tests | 283 pass (3 skipped) | 301 pass (0 skipped) — 43 BMONI tests removed, 61 added |
| Migration drift | none | none |
| `manage.py check --deploy` (real-length key) | not run | clean |
| Frontend production build | not run | builds |
| `pip-audit` | 3 CVEs (djangorestframework 3.18.1) | clean (3.18.2) |
| `npm audit` runtime deps | 1 high, 2 moderate | 2 moderate (react-router 6, see §5) |
| CI | none | `.github/workflows/ci.yml` |

Commands: `python manage.py test`, `python manage.py makemigrations --check --dry-run`,
`python manage.py check --deploy`, `pip-audit -r requirements.txt`,
`npm ci && npm run build`, `npm audit --omit=dev`.

**Not tested:** any call to the real Paystack API (all gateway calls are
mocked), PostgreSQL (tests ran on SQLite, so row locks were not exercised),
the Render/Vercel deployment itself, load. In a local browser I exercised
only: executive login and dashboard, the handover form (wrong and right
password), and the new-session preview. Other screens were not clicked through.

## 4. What changed and why

### Payment integrity
| Finding | Fix | Locked by |
|---|---|---|
| `verify` re-asked Paystack about settled payments and could flip a `success` to `failed` | Only a `pending` online payment is sent to the gateway; a settled row is returned as-is | `test_verify_never_asks_the_gateway_about_a_settled_payment` |
| Webhook and verify could settle the same row at once | Settlement runs under `select_for_update` in one transaction; the unique index stays as backstop | existing race test still passes |
| Currency was never checked | A charge that is not NGN fails into refund review | `test_right_amount_in_the_wrong_currency_is_not_credited` |
| Student with no email got a vague 502 | Clear `400 email_required`, no gateway call | `test_online_payment_needs_an_email` |
| A fee's amount/level could be edited after people paid (drop price, pay, raise it back) | Amount, level and department freeze once a non-failed payment exists | `test_fee_price_is_frozen_once_someone_has_paid` |
| Deleting a student or department cascade-deleted payments; deleting a fee orphaned them | `PROTECT` on `Payment.student`, `Payment.contribution`, `Contribution.department`; Django admin can no longer add/delete payments or edit money fields | `test_a_student_or_fee_with_payments_cannot_be_deleted` |

### Access control
| Finding | Fix | Locked by |
|---|---|---|
| A class rep could approve their own offline payment | Nobody reviews their own payment; it is also hidden from their queue | `test_rep_cannot_approve_their_own_offline_payment` |
| A rep with no department saw every department's review queue | Empty queue | `test_rep_without_a_department_sees_no_queue` |
| A rep could issue a reset code for another rep or admin and take the account over | Reps can reset only students in their own department and level | `test_rep_can_reset_a_student_but_not_another_rep` |
| Password reset left the old login token valid | Token is revoked on reset | `test_password_reset_revokes_the_old_token` |
| A rep could change their own level and act on another class | A rep's level is admin-set | `test_rep_cannot_move_themselves_to_another_level` |
| `set-role` could demote another admin | Blocked | `test_an_admin_cannot_be_demoted_through_set_role` |

### Uploads, config, hygiene
- **Proof files** are size-limited (5 MB), type-limited (JPG/PNG/WEBP/PDF),
  stored under a random name, and served only through the new authenticated
  `GET /api/payments/{id}/proof/` (owner, or a reviewer in scope). Previously
  they were reachable only when `DEBUG=True`, i.e. not at all in production.
- **`SECURE_PROXY_SSL_HEADER`** added. Without it, `SECURE_SSL_REDIRECT`
  behind Render's proxy redirects every request — including the Paystack
  webhook — forever. (Confirmed by reading the settings, not by deploying.)
- Production refuses to boot on a placeholder `SECRET_KEY` or Paystack key.
- A production frontend build refuses to run on mock data.
- **BMONI removed** (client, endpoints, models, settings, tests, docs) — it is
  excluded by the project directive and was a holder-named account outside the
  direct-collection model. The frontend never used it.
- `dept-payments/` removed: an older copy of `frontend/`.
- Duplicate code in the payments views collapsed; behaviour unchanged.

### Migration note — read before deploying
`payments/0008` **drops the two BMONI tables**. They only ever held sandbox
data, but if a deployed database has rows you want, export them first.
Everything else in the two new migrations is a constraint change with no data
movement. Rollback: `python manage.py migrate payments 0007 && python manage.py migrate contributions 0002`.

## 5. Open items (release blockers first)

| # | Item | Why it blocks | Smallest fix |
|---|---|---|---|
| 1 | Proof files need a persistent disk | **Half done.** `MEDIA_ROOT` is now configurable, but the disk itself is infrastructure: Render's free disk is wiped on every deploy | Paid plan with a persistent disk mounted at `MEDIA_ROOT` |
| 2 | ~~Rate limits are per-process memory~~ | **Fixed.** Counters are in the database (shared by all workers); 10/min per targeted account, 120/min per IP | — |
| 3 | No live Paystack test | Mocks prove our logic, not the integration | Run the manual guide end-to-end on **test** keys in staging, including a real webhook delivery |
| 4 | ~~One claim code works for every unclaimed account~~ | **Fixed.** A code only claims accounts from its own import and expires after 30 days. Re-importing the roster issues a fresh code to anyone who has not claimed yet | — |
| 5 | ~~Open self-registration~~ | **Fixed.** `ALLOW_SELF_REGISTRATION` switch; the Render blueprint sets it off | — |
| 6 | ~~Roster import has no size cap and is not atomic~~ | **Fixed.** 1 MB / 5,000-row cap, field validation, all-or-nothing | — |
| 3a | **The live Paystack secret key was shared in a chat** | Anyone with it can forge webhooks and move money through the account | Roll the key in the Paystack dashboard before going live; put the new one only in the host's secret settings |
| 7 | Refunds are recorded by hand | `refund_status` is set in the admin; Paystack refund/dispute events are not consumed | Handle `refund.processed` and `charge.dispute.*` webhooks |
| 8 | No settlement reconciliation | A successful charge is not proof the money reached the institution's bank | Periodic comparison against Paystack's settlement API |
| 9 | Students can still change their own level | They can drop out of a level-targeted fee's "who owes" list. Left as-is because three contract tests define it as intended | **Owner decision** — see §7 |
| 10 | Tokens never expire and sit in `localStorage` | A stolen token is valid until logout or reset | Token age limit |
| 11 | react-router 6 advisories (2 moderate) | Fix needs the v7 major upgrade | Planned upgrade with UI regression pass |
| 12 | Free Render Postgres | No backups; deleted 30 days after creation | Paid plan, or Neon/Supabase, before real data |

Smaller items from the review (N+1 queries in analytics and the fee list, no
pagination, container runs as root, read endpoints not level-scoped for reps)
are tracked in the review notes and are not launch blockers at pilot scale.

**Needs people, not code:** NDPA/NDPC data-protection review (the app stores
names, matric numbers, emails, phone numbers and payment evidence), the
institution's sign-off on the refund procedure, and Paystack live-mode
activation with the institution's settlement account. Nothing in this report
is a claim of legal, regulatory or PCI DSS compliance.

## 6. Where to deploy

**Free, for a demo**
| Piece | Option | Catch |
|---|---|---|
| Backend | Render free web service (blueprint already in `render.yaml`) | Sleeps after ~15 min idle; ephemeral disk |
| Database | Render free Postgres | Deleted after 30 days, no backups |
| Database (longer-lived) | Neon or Supabase free Postgres — set `DATABASE_URL` | Connection and storage caps |
| Frontend | Vercel (config in `frontend/vercel.json`), Netlify or Cloudflare Pages | None that matter for a static SPA |

Free tiers change; check current terms before relying on one.

**Later, for production** — the app is a single stateless container plus
Postgres, so it moves without code changes:
- *Stay simple:* Render paid web service + paid Postgres + persistent disk.
- *AWS:* App Runner or ECS Fargate for `backend/Dockerfile`, RDS PostgreSQL,
  S3 for proofs, S3 + CloudFront for the frontend. Worth it once you need
  private networking, managed backups with point-in-time recovery, or audit
  requirements the institution imposes — not before.

A one-department pilot does not need AWS.

## 7. Decisions for the owner

1. **Student level changes.** Lock it (admin/roster only) or keep it
   self-service? Locking is safer for "who owes what"; self-service avoids an
   admin step when the roster is wrong. The check lives in
   `UserProfileSerializer.validate_level` (`backend/apps/users/serializers.py`).
2. **Overpayment.** Today any mismatch fails and goes to refund review. That
   is the conservative choice; keep it unless the institution says otherwise.
3. **Public registration** on or off in production (item 5).

## 8. Next steps, in order

1. Review and merge this branch through a pull request.
2. Deploy to staging on Paystack **test** keys; run `docs/MANUAL_TESTING_GUIDE.md`.
3. Roll the exposed Paystack live key; attach a persistent disk for proofs.
4. Institutional and data-protection sign-off; Paystack live activation.
5. Pilot with one department before opening wider.

## 9. Roles, handover and the academic year (added 2026-11-13)

| Role | Scope | Notes |
|---|---|---|
| Student | Own fees and payments | — |
| Class rep | Own department **and** level | Still owes dues |
| Department executive (e.g. financial secretary) | Whole department, every level | Still owes dues; can reset students and class reps |
| Admin | Everything | Appoints office holders; starts sessions; exempt from dues |

**Nobody approves or marks their own payment**, whatever their role.

**Handover** — `POST /api/auth/handover/` `{matric_number, password}`. A class
rep or executive passes the office to a claimed student in their department
(same level, for a rep), confirmed with their own password. Both role changes
happen in one transaction and each is written to `RoleChange`. An admin can
also appoint or remove an office holder with `set-role`.

**New session** — `POST /api/auth/sessions/` `{name, dry_run}` (admin).
- Every class moves up one level; class reps move with their class.
- Students at their department's `final_level` (default 400) graduate.
- A graduating office holder loses the office (audited).
- Every open fee is closed, so last session's fees cannot bill the new class.
- **Unpaid mandatory dues are carried over.** Before anyone moves, each
  student who still owes a mandatory fee that is still **open** (not closed by
  the department) and was raised since the previous session gets a `Debt` row. The fee stays
  visible and payable for that student in every later session and after
  graduation, until an ordinary payment (online, offline proof, or a
  mark-paid by an executive/admin) settles it. Optional fees just close.
- A past session's fee is frozen: its roster is the recorded debtors plus
  whoever paid, and it cannot be edited or reopened.
- A session name can be started once only; `dry_run` reports the counts first.

**Graduates** keep their login, payment history and receipts. They are
billed nothing new, but still see and can pay any debt carried over from
their time as a student.

### What this does not do yet
- A carried-over fee can be marked paid by an executive or admin; a class rep
  is only matched to it if their level equals the fee's original level.
- There is no way to write off or waive a debt yet; that is an admin
  decision to design with the department.
- `Department.final_level` can only be changed in the Django admin.
- A handover cannot be undone by the old holder; an admin reverses it.
- There is no screen listing `RoleChange` history; it is in the Django admin
  database only (the model is not yet registered there).
- Repeating a year, or a student changing department, is an admin edit.

## 10. Who pays Paystack's fee (added 2026-11-13)

Owner decision: **the student pays it**, so the department receives the full
fee. Controlled by `PAYSTACK_PASS_FEE_TO_STUDENT` (on in `render.yaml`).

- Paystack's published local rule: 1.5% + ₦100, the ₦100 waived under
  ₦2,500, capped at ₦2,000 — and the fee applies to the passed-on amount too.
  The charge is therefore grossed up, rounding up to the kobo
  (`backend/apps/payments/fees.py`). A ₦5,000 fee is charged as ₦5,177.67.
- `Payment.amount` stays the fee (what totals and rosters count);
  `Payment.charge_amount` is what the gateway was asked to collect;
  `paid_amount` is what it reported. Only the exact charge settles a payment.
- Students see the online total before paying (`online_amount` on each fee).
  Offline payments are the fee only.
- A test sweeps every naira amount to ₦3,000 and a range up to ₦300,000 and
  asserts the department never nets less than the fee and the student is
  never overcharged by more than 5 kobo.

**Watch for:** the four pricing numbers are settings. If Paystack changes its
rates, or the account has a negotiated rate, update them — otherwise the
department nets slightly less (or students pay slightly more) than intended.
International cards are priced differently by Paystack and are not handled.
This has not been checked against a real Paystack settlement report.
