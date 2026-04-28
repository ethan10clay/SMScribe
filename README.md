![SMScribe](logo.png)

SMScribe is a lecture transcription service that works over text message. Send an audio recording by SMS, and get a clean transcript back on your phone. No app download, no complicated workflow, and no extra software to learn.

## Project Overview

SMScribe is intentionally simple from the user’s perspective, but the project itself covers the full product loop:

- A marketing site and account UI
- Phone-based login and account access
- SMS-based audio intake
- Subscription billing
- Transcript delivery and history

The core idea is to make transcription feel as lightweight as sending a text.

Technically, the project is a small event-driven system built around a few focused pieces:

- Static frontend pages for signup, pricing, and account management
- Serverless backend handlers for auth, billing, user data, and inbound SMS
- DynamoDB tables for users, jobs, and monthly usage
- An asynchronous Modal transcription worker that downloads media, runs Whisper, stores outputs, and sends results back by SMS

## What It Does

SMScribe is built for students and anyone else who wants fast lecture transcripts without leaving their messages app.

- Send an audio file by text
- Get a transcript delivered back by SMS
- View recent transcript history from your account
- Upgrade or manage your plan online

## How It Works

1. Sign up with your phone number.
2. Send your lecture audio file to SMScribe by text message.
3. SMScribe processes the recording and transcribes it.
4. Your transcript is sent back to you, along with a link when available.

## Who It’s For

SMScribe is especially useful for:

- Students recording lectures
- People who want searchable notes from spoken audio
- Anyone who prefers texting over installing another app

## Plans

SMScribe offers three tiers:

- Free: `3` transcriptions per month, up to `30` minutes per file
- Student: `30` transcriptions per month, up to `3` hours per file
- Pro: unlimited transcriptions, up to `6` hours per file

Paid plans are billed monthly.

## Best Experience

SMScribe is designed to feel simple:

- No app required
- No special recording workflow
- Everything happens through SMS and the web account page

iPhone tends to provide the smoothest experience for sending audio attachments. Android may work too, but some phones use default file formats that are less reliable with MMS/SMS attachments.

## Important Bits

- [frontend/index.html](/Users/ethantenclay/Desktop/SMScribe-1/frontend/index.html): the landing page, pricing, and signup flow
- [frontend/account.html](/Users/ethantenclay/Desktop/SMScribe-1/frontend/account.html): account management, usage tracking, billing access, and transcript history
- [backend/functions/twilio/webhook/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/twilio/webhook/handler.py): receives incoming SMS and audio submissions
- [backend/functions/auth/verify_start/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/auth/verify_start/handler.py) and [backend/functions/auth/verify_check/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/auth/verify_check/handler.py): phone verification and login
- [backend/functions/stripe/checkout/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/checkout/handler.py), [backend/functions/stripe/portal/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/portal/handler.py), and [backend/functions/stripe/webhook/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/webhook/handler.py): subscription checkout, billing management, and Stripe sync
- [backend/shared/db.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/shared/db.py): shared persistence logic for users, plans, usage, and transcript jobs
- [modal/transcriber.py](/Users/ethantenclay/Desktop/SMScribe-1/modal/transcriber.py): transcription worker logic

## Project Structure

```text
SMScribe-1/
├── backend/
│   ├── functions/
│   │   ├── auth/
│   │   ├── stripe/
│   │   ├── twilio/
│   │   └── user/
│   ├── shared/
│   └── setup/
├── frontend/
│   ├── index.html
│   ├── account.html
│   ├── privacy-policy.html
│   ├── terms.html
│   └── smscribe.css
├── modal/
│   └── transcriber.py
├── logo.png
└── README.md
```

## Arch At A Glance

- `frontend/` contains the public site and account pages users interact with
- `backend/functions/` contains serverless handlers for auth, billing, user data, and SMS workflows
- `backend/shared/` holds reusable backend logic, especially database and security helpers
- `modal/` contains the transcription-side processing code

Together, those pieces let SMScribe receive audio by text, process it, and return transcripts without asking the user to install anything.

## Request Flow

### 1. Authentication

SMScribe uses phone-number-based authentication instead of email/password accounts.

- The frontend starts verification through [backend/functions/auth/verify_start/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/auth/verify_start/handler.py)
- Verification is completed through [backend/functions/auth/verify_check/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/auth/verify_check/handler.py)
- After a successful check, the backend issues a signed JWT using helpers in [backend/shared/security.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/shared/security.py)
- Protected endpoints read `Authorization: Bearer <token>` and resolve the user from the token’s `sub`

This keeps account access aligned with the product itself: the phone number is both the user identity and the delivery channel.

### 2. Audio Submission

Once a user is registered, the main runtime path starts with Twilio.

- Twilio delivers inbound SMS/MMS requests to [backend/functions/twilio/webhook/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/twilio/webhook/handler.py)
- The webhook first validates the Twilio signature before doing any processing
- It checks whether the sender already has an account in DynamoDB
- It accepts media only if the attachment type or file extension looks like supported audio/video input
- Before transcription starts, it enforces monthly plan limits with `check_plan_limit()` and increments usage atomically

If the message is not a media upload, the same webhook also handles lightweight commands such as `HELP`, `STATUS`, and `SUPPORT`.

### 3. Transcription Pipeline

The transcription step is intentionally decoupled from the Twilio webhook so the SMS request can return quickly.

- The Twilio webhook posts a job request to the Modal endpoint defined in [modal/transcriber.py](/Users/ethantenclay/Desktop/SMScribe-1/modal/transcriber.py)
- The Modal worker downloads the source media from Twilio
- It stores the original audio in S3
- It creates or updates a job record in DynamoDB
- It runs `faster-whisper` on the uploaded file
- It writes the finished transcript back to S3 and generates a presigned download URL
- It sends the result back to the user by SMS with a preview and transcript link

This split is one of the most important architectural choices in the project: the messaging webhook stays small and responsive, while the expensive transcription work runs asynchronously in a dedicated compute environment.

### 4. Billing and Plans

Billing is handled through Stripe-backed monthly subscriptions.

- [backend/functions/stripe/checkout/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/checkout/handler.py) creates subscription checkout sessions
- [backend/functions/stripe/portal/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/portal/handler.py) opens Stripe’s billing portal for cancellations and payment updates
- [backend/functions/stripe/webhook/handler.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/functions/stripe/webhook/handler.py) synchronizes subscription state back into the user record

Plan enforcement is not just a UI concern. The Twilio webhook checks usage limits on inbound submissions, so free-tier or lower-tier users cannot bypass the frontend and send unlimited files directly by text.

## Data Model

The shared persistence layer in [backend/shared/db.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/shared/db.py) is one of the key pieces of the project.

It centers around three DynamoDB tables:

- `users`: stores the phone number as the primary identity, the current plan, Stripe identifiers, consent text, and timestamps
- `jobs`: stores per-transcription state such as status, audio key, transcript key, duration, word count, error state, and presigned transcript URL
- `usage`: stores a per-user, per-month transcription counter used for plan enforcement

There are also a few important implementation details here:

- Monthly limits are represented centrally in `PLAN_LIMITS`
- Usage increments are done with atomic DynamoDB `ADD` updates
- Failed transcription starts can roll usage back with a best-effort decrement
- Job history is queried through a phone-number secondary index for the account dashboard

## Security Notes

The security model is intentionally lightweight but still practical for a project of this size.

- JWTs are implemented directly in [backend/shared/security.py](/Users/ethantenclay/Desktop/SMScribe-1/backend/shared/security.py) using HMAC-SHA256 rather than a large auth framework
- Twilio requests are validated with Twilio signature verification before inbound SMS is trusted
- Stripe webhooks are validated before billing state is updated
- CORS is restricted to the configured frontend origin

That combination keeps the backend fairly small while still protecting the highest-risk paths: login, billing, and inbound webhook trust.

## Frontend Notes

The frontend is deliberately minimal and mostly serverless-friendly.

- [frontend/index.html](/Users/ethantenclay/Desktop/SMScribe-1/frontend/index.html) is the main marketing surface and first-run signup experience
- [frontend/account.html](/Users/ethantenclay/Desktop/SMScribe-1/frontend/account.html) is a lightweight authenticated dashboard driven by API calls
- [frontend/smscribe.css](/Users/ethantenclay/Desktop/SMScribe-1/frontend/smscribe.css) defines the shared visual system

There is no heavy SPA framework here. The UI is mostly static HTML, CSS, and browser-side JavaScript calling small backend endpoints. That keeps the project easy to host and easy to reason about.

## Account Features

From your account page, you can:

- View your current plan
- Check monthly usage
- Review recent transcript history
- Manage billing
- Delete your account if needed

## Billing Notes

- Paid subscriptions renew automatically each month
- You can cancel at any time
- Cancellation takes effect at the end of the current billing period
- Purchases are non-refundable

## Privacy

SMScribe uses your phone number to operate the service and deliver transcripts and account-related messages. Payment processing is handled securely through Stripe.

For the latest legal details, see:

- [Privacy Policy](frontend/privacy-policy.html)
- [Terms & Conditions](frontend/terms.html)

## Support

If you need help with SMScribe, contact:

- `support@smscribe.com`
