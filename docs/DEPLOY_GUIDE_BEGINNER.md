# Deploy guide for beginners (copy-paste for WhatsApp or Slack)

Everything below the line is written to be pasted straight into a chat.
It uses free plans and Paystack **test** keys. Going live is the last step
and is a key swap, nothing else.

---

*DPT Pay — how to put it online (about 45 minutes, no coding)*

You need 4 free accounts. Sign in to each with GitHub where it offers:
GitHub, Neon (database), Render (backend), Vercel (website). Plus your Paystack dashboard.

*STEP 1 — Get the code onto the main branch*
1. Open github.com/Equipe344/Ledger
2. Click *Pull requests* → *New pull request* → choose `chore/production-readiness` → *Create pull request* → wait for the green tick → *Merge*.

*STEP 2 — Create the database (Neon)*
1. Go to neon.com → *Sign up* → *Create project*. Name it `dpt`. Pick the region closest to you.
2. On the project page click *Connect*. Copy the connection string. It starts with `postgresql://` and ends with `sslmode=require`.
3. Paste it into a private note for now. This is your *DATABASE_URL*. Treat it like a password.

*STEP 3 — Get your Paystack TEST keys*
1. Paystack dashboard → make sure the switch at the top says *Test mode*.
2. *Settings → API Keys & Webhooks*. Copy the *Test Secret Key* (`sk_test_...`) and *Test Public Key* (`pk_test_...`).

*STEP 4 — Put the backend online (Render)*
1. render.com → *New +* → *Blueprint* → choose the *Ledger* repo → *Connect*.
2. Render reads the project and asks you to fill in some values:
   • *DATABASE_URL* → the Neon string from Step 2
   • *PAYSTACK_SECRET_KEY* → `sk_test_...`
   • *PAYSTACK_PUBLIC_KEY* → `pk_test_...`
   • *ADMIN_PASSWORD* → make up a strong password. This is your admin login.
   • *ADMIN_EMAIL* → your email
   • *CORS_ALLOWED_ORIGINS* → type `https://example.com` for now (we fix it in Step 6)
   • *FRONTEND_URL* → type `https://example.com` for now
   • *MEDIA_ROOT* → leave empty
3. Click *Apply*. Wait 5–10 minutes until it says *Live*.
4. Copy the address Render gives you, like `https://dpt-backend.onrender.com`. This is your *BACKEND address*.
5. Check it: open `BACKEND address/api/health/` in your browser. You should see a short "ok" message. (First load can take up to a minute on the free plan.)

*STEP 5 — Put the website online (Vercel)*
1. vercel.com → *Add New → Project* → import the *Ledger* repo.
2. *Root Directory* → click *Edit* → choose `frontend`.
3. Open *Environment Variables* and add one:
   • Name: `VITE_API_BASE_URL`
   • Value: your BACKEND address with `/api` on the end, e.g. `https://dpt-backend.onrender.com/api`
4. Click *Deploy*. When it finishes, copy the address, like `https://ledger-abc.vercel.app`. This is your *WEBSITE address*.

*STEP 6 — Introduce the two to each other*
1. Back in Render → your service → *Environment*.
2. Change *CORS_ALLOWED_ORIGINS* to your WEBSITE address. No slash at the end.
3. Change *FRONTEND_URL* to the same WEBSITE address. No slash at the end.
4. *Save changes*. Render restarts by itself (2–3 minutes).

*STEP 7 — Tell Paystack where to report payments*
1. Paystack → *Settings → API Keys & Webhooks* (still in Test mode).
2. *Test Webhook URL* → `BACKEND address/api/payments/webhook/` → *Save*.
   Without this, payments will not be marked as paid automatically.

*STEP 8 — Try it*
1. Open your WEBSITE address. Log in with username `admin` and the ADMIN_PASSWORD you chose.
2. *Import roster* → upload a small CSV with the columns `first_name,last_name,matric_number,level,department,email` (2 or 3 classmates is enough; the department must be spelled exactly as it appears in the app). Copy the claim code it shows.
3. *Contributions* → create a fee, e.g. ₦500.
4. Open the site in a private/incognito window → *Claim a roster account* → use a matric number, first name and the claim code → set a password → log in.
5. Pay the fee. On the Paystack page use Paystack's test card `4084 0840 8408 4081`, any future date, CVV `408`. No real money moves.
6. You should land back on the site with a "paid" receipt, and the admin dashboard should show that student as paid.

If step 6 works, the whole system works.

*STEP 9 — Going live (only after Step 8 works)*
1. Paystack → switch to *Live mode* → copy the *Live* keys → set the *Live Webhook URL* to the same `BACKEND address/api/payments/webhook/`.
2. Render → *Environment* → replace PAYSTACK_SECRET_KEY and PAYSTACK_PUBLIC_KEY with the live ones → *Save*.
3. Pay one small real fee yourself first and confirm the money shows in Paystack before telling students.

*Good to know*
• Free Render goes to sleep after 15 minutes. The first visitor waits up to a minute. A paid plan removes this.
• On the free plan, uploaded payment proofs (screenshots) are erased whenever the backend is redeployed. Online Paystack payments are not affected. For real use, upgrade Render and add a disk.
• The Neon database does not expire, but the free plan has size limits — plenty for one department. Check neon.com/pricing for the current numbers.
• Never paste keys or the database string into a group chat. Only into the Render settings page.
• Something broke? Render → your service → *Logs* shows the reason. Screenshot it and send it to the developer.
