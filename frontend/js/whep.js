// WHEP playback of one Feed, through the backend's Playback Gateway.
//
// Non-trickle: gather this browser's ICE candidates, send one offer, get one
// answer carrying the Relay's candidates. Media then flows from the Relay's
// WebRTC port straight to this <video>. Receive-only, video only.

import { cameraPath, toLoginWall } from './api.js';

const GATHER_TIMEOUT_MS = 2000;

// Resolves once gathering completes, after GATHER_TIMEOUT_MS, or on `signal`'s
// abort, whichever comes first.
function gatheringComplete(pc, signal) {
  if (pc.iceGatheringState === 'complete' || signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      signal?.removeEventListener('abort', done);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    pc.addEventListener('icegatheringstatechange', check);
    signal?.addEventListener('abort', done, { once: true });
    const timer = setTimeout(done, GATHER_TIMEOUT_MS);
  });
}

export class FeedError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

// Start playing a Camera's Feed into a <video>. Resolves to a handle whose
// close() ends the session on both ends.
//
// Aborting `signal` while the session is still opening closes its peer
// connection at once and rejects with the signal's reason. An offer already
// sent is left to finish, so its late answer's session can be ended on the
// Relay too; one not yet sent is never sent.
export async function playFeed(video, cameraId, { signal } = {}) {
  signal?.throwIfAborted();
  const pc = new RTCPeerConnection();
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.addEventListener('track', (event) => {
    video.srcObject = event.streams[0] ?? new MediaStream([event.track]);
    // `autoplay` alone is not enough: Chromium can leave a muted autoplay
    // video paused when it was inside a hidden view moments before.
    video.play().catch(() => {});
  });

  let location = null;
  const close = () => {
    pc.close();
    if (location) fetch(location, { method: 'DELETE', keepalive: true }).catch(() => {});
  };

  const abort = () => pc.close();
  signal?.addEventListener('abort', abort, { once: true });
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await gatheringComplete(pc, signal);
    signal?.throwIfAborted();
    const response = await fetch(`/api/feeds/${cameraPath(cameraId)}/whep`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: pc.localDescription.sdp,
    });
    if (response.status === 401) toLoginWall();
    if (response.status !== 201) {
      let body = {};
      try { body = await response.json(); } catch { /* not JSON */ }
      throw new FeedError(body.message ?? `HTTP ${response.status}`, body.error ?? 'feed_unavailable');
    }
    location = response.headers.get('Location');
    signal?.throwIfAborted();
    await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() });
    signal?.throwIfAborted();
  } catch (error) {
    close();
    throw signal?.aborted ? signal.reason : error;
  } finally {
    signal?.removeEventListener('abort', abort);
  }
  return { close, peerConnection: pc };
}

const DISCONNECT_GRACE_MS = 3000;

// Call lost() once when a playing Feed's peer connection drops for good:
// failed, or disconnected for longer than a brief network blip. The Relay
// closes its side when the Feed's source dies, which the browser sees as a
// disconnect a few seconds later.
export function onConnectionLost(pc, lost) {
  let timer = null;
  let done = false;
  const fire = () => {
    if (done) return;
    done = true;
    pc.removeEventListener('connectionstatechange', check);
    lost();
  };
  const check = () => {
    const state = pc.connectionState;
    if (state === 'failed') fire();
    else if (state === 'disconnected') timer ??= setTimeout(fire, DISCONNECT_GRACE_MS);
    else if (state === 'connected') { clearTimeout(timer); timer = null; }
  };
  pc.addEventListener('connectionstatechange', check);
}

const RETRY_MS = 2000;

// Play a Feed and keep it playing until close(): a Feed that fails to open,
// or whose connection is lost, is re-opened every RETRY_MS. Asking again is
// what keeps the Feed wanted while it is on screen, so the instance reports a
// dead source Down rather than Idle, and the Feed plays again by itself once
// its Device recovers. onMessage gets a short status line ('' once playing).
// The returned handle's `session` is the current playFeed handle, or null.
// close() also aborts a session still opening, so a page that swaps Feeds at
// any moment holds at most one peer connection.
export function keepPlaying(video, cameraId, { onMessage = () => {} } = {}) {
  let closed = false;
  let session = null;
  let retry = null;
  const opening = new AbortController();

  const again = (text) => {
    onMessage(text);
    retry = setTimeout(connect, RETRY_MS);
  };

  async function connect() {
    try {
      const opened = await playFeed(video, cameraId, { signal: opening.signal });
      if (closed) {
        opened.close();
        return;
      }
      session = opened;
      video.addEventListener('playing', () => { if (!closed) onMessage(''); }, { once: true });
      onConnectionLost(opened.peerConnection, () => {
        if (closed) return;
        session?.close();
        session = null;
        again('Feed lost. Reconnecting…');
      });
    } catch (error) {
      if (!closed) again(`Feed unavailable: ${error.message}. Retrying…`);
    }
  }

  onMessage('Connecting…');
  connect();
  return {
    close() {
      closed = true;
      clearTimeout(retry);
      opening.abort();
      session?.close();
      session = null;
    },
    get session() { return session; },
  };
}
