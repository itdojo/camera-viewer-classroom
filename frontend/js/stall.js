// Stall: a Feed frozen in this browser while the Relay still reports it Live.
// A problem on the browser's own leg (Wi-Fi, a starved tab), so it is never a
// Feed Status and never reaches the server: other browsers can't see it.
//
// watchForStall puts an overlay over a <video> and polls the video's decoded
// frame count. Once the current session has decoded a frame, no new frame for
// the threshold while the Feed is Live shows the overlay; the next frame
// clears it. The clock restarts whenever the session changes (keepPlaying
// re-opens a Feed), the Feed becomes Live again, or the video is paused or the
// tab hidden, since the browser is then not decoding by choice. Before a
// session's first frame there is no Stall: keepPlaying's own message
// ("Connecting…") covers that time, and a Device with a long keyframe interval
// would otherwise flash Stalled on every open.
//
// The threshold is the instance's FEED_STALL_SECONDS, which reaches every
// browser in the `settings` event (`stall_seconds`); 2 s until it arrives.

const DEFAULT_STALL_SECONDS = 2;
const POLL_MS = 250;

export function stallSeconds(store) {
  return store.get('settings')?.stall_seconds ?? DEFAULT_STALL_SECONDS;
}

function decodedFrames(video) {
  return video.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
}

// container: the positioned element holding the video, which the overlay
// covers. session(): the current playback session (null while none), re-read
// on every poll. Returns { setLive(bool), close() }.
export function watchForStall(container, video, { session, store }) {
  const overlay = document.createElement('div');
  overlay.className = 'stall';
  overlay.setAttribute('role', 'status');
  overlay.hidden = true;
  const label = document.createElement('span');
  label.className = 'stall-label';
  label.textContent = 'Stalled';
  const why = document.createElement('span');
  why.className = 'stall-why';
  overlay.append(label, why);
  container.append(overlay);

  let live = false;
  let watched = null; // the session the clock belongs to
  let frames = 0;
  let decoding = false; // this session has decoded a frame
  let since = performance.now(); // last progress, or last reason not to judge

  const restart = () => {
    frames = decodedFrames(video);
    decoding = false;
    since = performance.now();
  };

  const tick = () => {
    const now = performance.now();
    const current = session();
    if (current !== watched) {
      watched = current;
      restart();
    }
    const count = decodedFrames(video);
    if (count !== frames) {
      frames = count;
      decoding = count > 0;
      since = now;
    }
    if (!live || !watched || video.paused || document.hidden) since = now;
    const seconds = stallSeconds(store);
    const stalled = live && decoding && now - since >= seconds * 1000;
    if (stalled) {
      why.textContent = `No new frame for ${seconds} s. The Relay says Live: check this browser's connection.`;
      overlay.title = why.textContent;
    }
    overlay.hidden = !stalled;
  };

  const timer = setInterval(tick, POLL_MS);
  return {
    setLive(next) {
      if (next && !live) since = performance.now();
      live = next;
      tick();
    },
    close() {
      clearInterval(timer);
      overlay.remove();
    },
  };
}
