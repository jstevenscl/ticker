# Ticker FAQ

---

## General

**Q: Do I need to restart Dispatcharr after installing or updating Ticker?**

No. Dispatcharr 0.34 and later loads new plugin code as soon as it is installed or updated. If an overlay seems to be running old behavior after an update, use the **Reload Plugin** button on the Ticker page.

---

**Q: A channel was skipped with "stream profile is Proxy or Redirect". What does this mean?**

Ticker injects overlays by adding FFmpeg filter parameters to the channel's cloned stream profile. **Proxy** and **Redirect** profiles do not go through FFmpeg — they either pass the stream through Dispatcharr's internal proxy or redirect the client straight to the source URL. There is no FFmpeg process to inject a drawtext filter into, so Ticker cannot add an overlay to these channels.

To fix it:

1. In Dispatcharr, go to **Channels** and open the affected channel.
2. Under **Stream Profile**, select any FFmpeg-based profile (any profile that is not named **Proxy** or **Redirect**). If you're not sure which to use, the default FFmpeg profile works for most channels.
3. Click **Save**.
4. Re-run the Ticker enable action for that channel.

If many channels are affected at once, the likely cause is that Dispatcharr's system-wide default stream profile is set to Proxy or Redirect and those channels have no explicit profile assigned. Change the default in Dispatcharr **Settings → Streams** to an FFmpeg-based profile, then re-run the enable action.

---

**Q: Will enabling a ticker change my stream quality or original stream profile?**

No. Ticker clones your existing stream profile and injects the overlay filter into the clone. Your original profile is never modified. When you disable the ticker, the original profile is restored and the clone is deleted.

---

**Q: The overlay is not showing up after I enabled it. What should I check?**

1. Make sure you restarted Dispatcharr after the last install or update.
2. Make sure someone is actively watching the channel. Ticker only writes overlay data to channels that have active viewers.
3. Wait up to 30 seconds after enabling for the first data to load.
4. Try **Actions → Reload Poller** to restart the background data thread.
5. If still nothing, disable and re-enable the channel to re-clone the stream profile with current parameters.

---

## Sports Ticker

**Q: The ticker says "No games scheduled." Is something broken?**

No — this is normal. It means the ESPN API has no live or recently finished games for the leagues you have enabled right now. This happens during off-hours, between seasons, and on days with no scheduled games. The ticker will start showing scores automatically as soon as games begin. Check your league toggles to make sure the right sports are turned on.

---

**Q: How many games can the ticker show at once?**

The ticker scrolls approximately 600 characters of content per pass. Live games always appear first, followed by final scores. When many leagues are active and there are lots of games in progress, scores that don't fit in the first 600 characters will appear on the next loop — typically a few seconds later.

If you want to make sure specific teams are always visible regardless of how many games are happening, use the **Favorite Teams** field. When favorites are set, only games involving those teams are included in the ticker. See the [ESPN Team Abbreviations Reference](TEAMS.md) for a full list of abbreviations by league.

---

**Q: I have a lot of leagues enabled but I'm not seeing my team's game. What's happening?**

The ticker fits roughly 600 characters per scroll pass. If many leagues are active simultaneously, some games may not appear until the next loop. The fix is to enter your teams in the **Favorite Teams** field (comma-separated abbreviations, e.g. `KC, DEN, LAR`). With favorites set, only those teams' games are displayed — the ticker stays focused no matter how many leagues are active. See the [ESPN Team Abbreviations Reference](TEAMS.md) for a full list of valid abbreviations by league.

---

**Q: What's the difference between Single Color and Multi-Color mode?**

- **Single Color (default):** All ticker text is white. Uses one FFmpeg drawtext layer. Lower CPU usage, smoother on lower-powered systems, and recommended for most setups.
- **Multi-Color:** Sport/league labels (NFL:, NBA:, etc.) and team abbreviations are drawn in configurable colors, with scores in white. Uses three drawtext layers — approximately 3× more filter processing. Requires a monospace bold font in the container (DejaVu Mono or Liberation Mono); falls back to Single Color automatically if none is found.

If you are experiencing buffering on a sports ticker channel, try switching to Single Color mode (disable and re-enable the channel after changing the setting).

---

**Q: Why does Multi-Color require a monospace font?**

The three color layers (scores, abbreviations, labels) all scroll at the same speed because they share the same `text_w` measurement. That only works correctly if every character in each layer is the same width — a property guaranteed by monospace fonts. With a proportional font, the layers would have different widths and drift apart visually while scrolling.

---

**Q: The ticker was scrolling smoothly and then started jumping or changing length. What happened?**

This was a known issue in earlier versions caused by the ticker text changing length between ESPN poll cycles (every 30 seconds), which caused FFmpeg to recalculate the scroll width mid-scroll. It is fixed in v0.1.0: all ticker files are always exactly the same length, so the scroll width never changes between reloads.

If you're on v0.1.0 and still seeing this, disable and re-enable the channel to pick up the current parameters.

---

**Q: The stream buffers after enabling the sports ticker. What can I do?**

A few things to check:

1. **Try Single Color mode.** Multi-Color uses three drawtext layers which is more CPU-intensive. Switch to Single Color (disable → change Color Mode setting → re-enable).
2. **Check your base stream profile.** If the channel's original profile contains `-force_key_frames`, older versions of Ticker would inherit that and produce very high bitrate output. v0.1.0 strips this automatically.
3. **Check your VPN or network.** If Dispatcharr or your IPTV provider routes through a VPN, VPN instability can cause buffering on any transcoded stream. Try disabling the ticker and see if the stream runs clean — if it does and buffering returns only with the ticker, the VPN may be under load.
4. **Run Reload Poller.** A stuck background thread can cause file I/O issues that indirectly stress the encoder.

---

**Q: Can I put the sports ticker on an audio-only channel?**

Yes. Ticker automatically detects audio-only channels and injects a 1280×720 black video background so the overlay has somewhere to render. The result is a black screen with the scrolling ticker, which is normal and expected for radio-style channels.

---

## Custom Text

**Q: How do I change the text on an active Custom Text channel without interrupting the stream?**

Update the **Custom Text** field with your new message and run **Actions → Update Custom Text**. The new text appears immediately without disabling and re-enabling the channel.

---

**Q: What is the difference between Static and Scrolling style?**

- **Static:** The text stays centered at a fixed position on screen. Good for channel branding, station IDs, or short messages.
- **Scrolling:** The text scrolls horizontally across the screen from right to left. Good for longer messages or news-style tickers.

---

**Q: What does the Timed schedule do?**

Timed mode makes the overlay appear for a set number of seconds, disappear, then reappear on a repeating cycle. For example: Duration = 10 seconds, Interval = 5 minutes means the text shows for 10 seconds, hides for 4 minutes 50 seconds, then shows again for 10 seconds, and so on. Use this when you want a subtle periodic reminder rather than a persistent overlay.

---

**Q: Can I use Custom Text and Sports Ticker on the same channel at the same time?**

No. Each channel supports one active ticker at a time. To switch types, disable the current ticker and enable the new one.

---

## SiriusXM Now Playing

**Q: My SiriusXM channel is showing the channel name but no artist or song. Why?**

The overlay uses fallback content (the channel name) until the first successful poll of xmplaylist.com. This takes up to 15 seconds after enabling. If it never updates, the channel name may not match any station in the xmplaylist database. Run **Actions → Refresh Channel Data** to retry the matching. If still unmatched, the channel name may be too different from the xmplaylist station name to auto-match — this is a known limitation of fuzzy matching.

---

**Q: Does Ticker support SiriusXM video channels?**

Yes. The Now Playing overlay is designed for audio-only channels (where it injects a black video background), but it will also work on video channels. The overlay box will appear centered on the existing video.

---

**Q: Why is the Now Playing info sometimes a track or two behind?**

xmplaylist.com is a third-party service that scrapes SiriusXM. There is inherent delay between when a track starts playing and when xmplaylist reflects it, typically 30–60 seconds. This is a limitation of the data source, not Ticker.

---

## Satellite Radio Channel Setup

**Q: Why does Absolute numbering mode require a Numbering Block Size?**

Absolute mode sets each channel's trailing digits to its real SXM station number — but SiriusXM's full catalog runs up to station 1999, and channels with no match need a reserved range to fill into that won't drift into whatever you numbered next. Rather than guess a default, Ticker requires you to set it explicitly (1000 comfortably covers a typical lineup — real provider data shows ~99.8% of channels at or below 999). A matched channel whose real station number falls outside your block still gets its correct absolute number and is called out in the result message, never silently moved.

---

**Q: Is it safe to run Sort Channels multiple times with Prefix Channel Name enabled?**

Yes. Matching strips any existing station-number prefix before comparing against the SXM dataset, so a channel already renamed to "036 Alt Nation" still matches "Alt Nation" and stays correctly positioned on every subsequent run — including Assign Logos and Fill EPG, which use the same matching.

---

**Q: Two of my channels both matched the same station number. What happens?**

Only the first one keeps that station's real absolute number; the other is placed in the fill pool instead of colliding on the same channel number, and both are named explicitly in the result message so you can investigate (usually a duplicate SD/HD channel from your provider, or two channels that are genuinely the same station).

---

**Q: Where do channels with no station match end up?**

Right after your highest real matched station — never in the low numbers below it. SXM's own numbering starts at station 2, so those low slots are reserved for real stations and are never backfilled with unmatched or duplicate-loser content, even if they're technically free. This also applies to the losing side of a duplicate-name match above.
