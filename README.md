# QEOP & London Stadium Traffic Watch — Setup Guide

This is a calendar that fills itself in with event days at Queen Elizabeth
Olympic Park and London Stadium that might mean big crowds, road closures,
or parking restrictions. Once it's set up, you never have to touch it
again — it updates itself every week.

You don't need to know how to code to set this up. You just need to
copy-paste some things. It'll take about 15 minutes. Go slowly and it'll
work.

**What you're building, in plain terms:** GitHub (a free website for
storing files and running small automated jobs) will check the two event
websites every Monday, build an updated calendar file, and save it. Your
phone or computer's calendar app then "subscribes" to that file, the same
way you might subscribe to a public holidays calendar — it checks in on
its own and shows you what's new.

---

## Before you start

You need a free GitHub account. If you don't have one:

1. Go to [github.com](https://github.com)
2. Click **Sign up**, enter an email, password, and username
3. Verify your email when it asks

That's it — keep this tab open, you'll come back to it.

---

## Step 1: Create a place to store this project

1. On [github.com](https://github.com), click the **+** icon in the top
   right corner, then **New repository**
2. Under "Repository name" type: `qeop-ical`
3. Leave everything else as default
4. Click **Create repository**

You'll land on a mostly empty page for your new repository. Keep this tab
open too.

## Step 2: Upload the project files

You do **not** need to use the command line for this part.

1. On your new repository's page, click **uploading an existing file**
   (it's a blue link in the middle of the page — if you don't see it,
   look for **Add file → Upload files** near the top right)
2. Unzip the `qeop-ical.zip` file I gave you (double-click it on
   Mac/Windows and it'll extract itself into a folder)
3. Open that unzipped `qeop-ical` folder
4. Select **everything inside it** (all the files and folders — `.github`,
   `docs`, `generate_ics.py`, `requirements.txt`, `.gitignore`) and drag
   them all into the GitHub upload box in your browser

   ⚠️ Important: drag what's *inside* the `qeop-ical` folder, not the
   folder itself. GitHub needs to see `generate_ics.py` sitting at the top
   level, not inside another folder.

5. Scroll down, click the green **Commit changes** button

Your repository page should now show a list of files including
`generate_ics.py` and a `.github` folder.

## Step 3: Turn on the automatic weekly check

This is the part that makes it update itself.

1. On your repository page, click the **Actions** tab along the top
2. GitHub might show a message about workflows — click **I understand my
   workflows, go ahead and enable them**
3. You should now see something called **"Update QEOP traffic calendar"**
   listed on the left
4. Click on it, then click the **Run workflow** button (grey button, top
   right of the list), then click the green **Run workflow** button that
   appears in the little dropdown
5. Wait about 30 seconds, then refresh the page. You should see a run with
   a ✅ green tick next to it. That means it worked and it just built your
   first calendar file.

   If you see a ❌ red cross instead, click into it to see what went wrong,
   or just show me the error message and I'll help you fix it.

From now on, this same check will run automatically every Monday morning —
you don't need to do anything.

## Step 4: Make the calendar file visible/reachable

Your calendar app needs a web address (URL) to check. Let's turn one on.

1. On your repository page, click **Settings** (top right of the tabs)
2. In the left-hand menu, click **Pages**
3. Under "Build and deployment" → "Source", choose **Deploy from a
   branch**
4. Under "Branch", change the first dropdown to **main** and the second
   dropdown to **/docs**, then click **Save**
5. Wait about a minute, then refresh the page. Near the top it'll show
   something like:

   > Your site is live at `https://<your-username>.github.io/qeop-ical/`

6. Your calendar's actual address is that, plus `qeop-traffic.ics` on the
   end:

   ```
   https://<your-username>.github.io/qeop-ical/qeop-traffic.ics
   ```

   Replace `<your-username>` with your actual GitHub username. **Copy this
   full address somewhere** (Notes app, etc.) — this is the one link
   you'll need for the next step.

## Step 5: Subscribe to it in your calendar app

Pick whichever matches what you use:

### On an iPhone
1. Open **Settings** → scroll down to **Calendar**
2. Tap **Accounts** → **Add Account** → **Other**
3. Tap **Add Subscribed Calendar**
4. Paste in the address from Step 4, tap **Next**, then **Save**

The events will now show up in your Calendar app automatically, and your
phone will check for updates on its own every so often.

### On a Mac
1. Open the **Calendar** app
2. Click **File** (top menu bar) → **New Calendar Subscription**
3. Paste in the address from Step 4, click **Subscribe**
4. A settings box appears — next to "Auto-refresh", choose **Every week**
   (or **Every day** if you want to check more often than the file itself
   updates)
5. Click **OK**

### On Google Calendar (web browser)
1. Go to [calendar.google.com](https://calendar.google.com)
2. On the left side, next to "Other calendars", click the **+**, then
   **From URL**
3. Paste in the address from Step 4, click **Add calendar**

Google checks subscribed calendars roughly once a day on its own schedule
— you can't change that, but it's still more often than the weekly updates
this project makes, so you're covered either way.

---

## You're done

That's the whole setup. From here:

- Every **Monday morning**, GitHub automatically re-checks both event
  websites and updates the calendar file for you.
- Your calendar app checks that file on its own schedule and shows you
  whatever's new — no further action needed from you.
- Each event shows up as an all-day entry, with a `[LOW]`, `[MEDIUM]`, or
  `[HIGH]` tag in the title so you can tell at a glance how disruptive it
  might be, and the notes underneath it list the times and any known road
  closure or noise information.

## If something looks wrong later

**"I don't see any events in my calendar"**
Go back to your repository's **Actions** tab and check the most recent run
has a green tick. If it's red, click into it — it usually means one of the
event websites changed its layout slightly. Send me the error text from
that page and I'll fix the code for you.

**"I want it to check more than once a week"**
Tell me how often you'd like it (daily, twice a week, etc.) and I'll adjust
the schedule for you — it's one line in a settings file.

**"I want to change what counts as HIGH risk"**
Same thing — tell me what should count as higher or lower risk (e.g. "West
Ham matches should always be HIGH") and I'll update the rules.

You don't need to understand any of the code itself — just let me know
what you want changed and I'll make the edit.
