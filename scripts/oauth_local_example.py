"""How to mint the Gmail refresh token for the daily digest.

This file holds no code. The token is minted once, in the browser, with Google's
OAuth 2.0 Playground, and it is tied to your own OAuth client, so nobody else's
app ever holds it.

1. Google Cloud Console: create a project (or pick yours) and enable the
   Gmail API.

2. Google Auth Platform (the OAuth consent screen): fill in Branding, then
   under Audience choose user type External and click "Publish app", so the
   publishing status reads "In production". The app can stay unverified: for
   one user you only click through the "Google hasn't verified this app"
   screen (Advanced, then continue). Do not leave it in "Testing": refresh
   tokens of a Testing app expire after 7 days and the digest stops.

3. Google Auth Platform > Clients > Create client. Application type
   "Web application". Under "Authorized redirect URIs" add exactly
   https://developers.google.com/oauthplayground
   Save, then copy the client ID and the client secret.

4. Open https://developers.google.com/oauthplayground. In the gear menu (top
   right) tick "Use your own OAuth credentials" and enter that client ID and
   client secret. Without this step the token belongs to the Playground's own
   client: your client ID cannot refresh it, and the Playground revokes it
   after 24 hours.

5. Step 1 in the Playground: type the scope
   https://www.googleapis.com/auth/gmail.send
   and click "Authorize APIs". Sign in as the collector Gmail account (the one
   that sends the digest) and allow access. gmail.send can only send mail; never
   use https://mail.google.com/, which can also read and delete the mailbox.
   send_email.py warns on every run while the token has a broader scope, but
   it still sends.

6. Step 2: click "Exchange authorization code for tokens" and copy the
   refresh token.

7. GitHub > Settings > Environments > digest (create it, and under
   "Deployment branches and tags" allow only main). Add these five environment
   secrets (ANTHROPIC_API_KEY for the briefing lives in the same environment):
     GMAIL_CLIENT_ID      the client ID from step 3
     GMAIL_CLIENT_SECRET  the client secret from step 3
     GMAIL_REFRESH_TOKEN  the refresh token from step 6
     SENDER_EMAIL         the collector Gmail address
     RECIPIENT_EMAIL      the address that receives the digest
   Delete any copies under Settings > Secrets and variables > Actions, so only
   jobs on main that use the digest environment can read them.

8. Set the repository variable DIGEST_ENABLED to true (Settings > Secrets and
   variables > Actions > Variables), then run "Daily digest" once from the
   Actions tab with run: morning to check the secrets.

A token minted earlier with a broader scope keeps that scope. Revoke the app at
https://myaccount.google.com/permissions (signed in as the collector account)
and repeat steps 4 to 7. Never paste these values into an issue, a commit or a
chat; if one leaks, revoke it there and mint a new one.
"""
