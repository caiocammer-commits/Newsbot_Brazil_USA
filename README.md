# newsbot

Texts you a 9 AM Eastern digest of top US and Brazil news, plus breaking-news
alerts during the day. Summaries and headlines only, no links.

## Setup (about 10 minutes)

1. Install Python 3.9+ and, in this folder, run:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill it in:
   - `ANTHROPIC_API_KEY` from console.anthropic.com (writes the summaries and
     decides what counts as breaking; costs pennies per day with the default
     small model).
   - A text provider. For real SMS, make a Twilio account, buy a number, and set
     `SMS_PROVIDER=twilio` with the four Twilio values. For a free alternative,
     set `SMS_PROVIDER=ntfy`, install the ntfy app on your phone, and subscribe
     to your topic (this is a push notification, not an SMS).
3. Test it:
   ```
   python newsbot.py --test-sms
   python newsbot.py --digest-now
   ```
4. Run it:
   ```
   python newsbot.py
   ```

## How it behaves

- 9:00 AM Eastern every day (handles daylight saving automatically): one digest,
  5 US items and 5 Brazil items, translated into English.
- Every 10 minutes: checks the feeds for new stories. An AI editor decides if any
  are major breaking news. It is strict, so most checks send nothing.
- Guardrails: max 6 alerts a day, no alerts 11 PM to 7 AM (overnight news rolls
  into the morning digest), and no repeat alerts for the same event.
- The first run only records what is already out there, so you will not get a
  burst of old stories.
- If your computer is asleep or off at 9:00, the digest sends when it wakes up
  (within 3 hours of 9:00). Alerts only happen while the computer is awake.

## Keep it running

- **Windows:** Task Scheduler, "Create Task", trigger "At log on", action
  `pythonw.exe C:\path\to\newsbot.py`, and tick "Run whether user is logged on or not".
- **Mac:** a launchd agent, or simply run `nohup python3 newsbot.py &` in a terminal
  you leave alone.
- **Linux:** a systemd user service, or `nohup python3 newsbot.py &`.

Logs go to `newsbot.log` in this folder.

## Notes

- News sources are public RSS feeds (Google News, NPR, BBC, NYT, G1, Folha,
  Agencia Brasil, BBC Brasil). Edit the `FEEDS` dict at the top of `newsbot.py`
  to add or remove outlets. A feed that goes down is skipped automatically.
- US carriers have been shutting down free email-to-text gateways, which is why this
  uses Twilio (or ntfy) instead.
- Without an Anthropic key, the digest still works as a plain headline list, but
  breaking alerts are disabled.
