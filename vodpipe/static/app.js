'use strict';

const $ = sel => document.querySelector(sel);
// Corrupt local storage must not stop the dashboard booting.
function restoreExpanded() {
  try {
    const stored = JSON.parse(localStorage.getItem('expanded') || '[]');
    return new Set(Array.isArray(stored) ? stored.filter(v => typeof v === 'string') : []);
  } catch {
    localStorage.removeItem('expanded');
    return new Set();
  }
}

const state = {
  data: null,
  expanded: restoreExpanded(),
  outputs: new Map(),
  config: null,
  pollMs: 2000,
  polling: false,
  lastRefresh: null,
  refreshError: null,
};

/* ------------------------------------------------------------------ helpers */

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({ error: 'bad response' }));
  if (!response.ok || payload.error) throw new Error(payload.error || response.statusText);
  return payload;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('err', isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 7000 : 3200);
}

function guard(promise) {
  return promise.then(() => refresh()).catch(err => toast(err.message, true));
}

const pad = n => String(Math.floor(n)).padStart(2, '0');

function clock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  return `${pad(seconds / 3600)}:${pad((seconds % 3600) / 60)}:${pad(seconds % 60)}`;
}

function parseClock(text) {
  const cleaned = String(text).trim();
  if (!cleaned) return null;
  const parts = cleaned.split(':').map(Number);
  if (parts.some(Number.isNaN)) return null;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function bytes(count) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (count >= 1024 && index < units.length - 1) { count /= 1024; index += 1; }
  return `${count.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function when(epoch) {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function renderConnection() {
  const node = $('#connection');
  const refreshed = state.lastRefresh
    ? new Date(state.lastRefresh).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      })
    : 'never';
  if (state.refreshError) {
    node.className = 'connection stale';
    node.textContent = `Disconnected · last successful refresh: ${refreshed}`;
    node.title = state.refreshError;
    document.body.classList.add('poll-stale');
  } else if (state.lastRefresh) {
    node.className = 'connection connected';
    node.textContent = `Last refresh: ${refreshed}`;
    node.title = 'Dashboard data is current';
    document.body.classList.remove('poll-stale');
  } else {
    node.className = 'connection connecting';
    node.textContent = 'Connecting…';
    node.removeAttribute('title');
  }
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

// A re-render mid-typing would throw away what the user is entering.
const isEditing = () => ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);

/* ------------------------------------------------------------------ channels */

function renderChannels(data) {
  const root = $('#channels');
  root.textContent = '';
  if (!data.channels.length) {
    root.append(el('div', { class: 'empty', text: 'No channels yet. Add one above — any channel, nothing is hardcoded.' }));
    return;
  }

  for (const channel of data.channels) {
    // Armed = the user pressed Record while the channel was offline. Nothing is
    // running; the watcher starts it the moment the channel goes live.
    const unknown = channel.live_state === 'unknown';
    const dotClass = channel.recording
      ? 'dot rec'
      : (channel.armed ? 'dot armed' : (channel.live ? 'dot live' : (unknown ? 'dot unknown' : 'dot')));
    const statusText = channel.recording
      ? 'recording'
      : (channel.armed ? 'waiting for it to go live' : (channel.live ? 'live' : (unknown ? 'status unknown' : 'offline')));

    const toggle = el('input', { type: 'checkbox', id: `auto-${channel.name}` });
    toggle.checked = channel.auto_record !== false;
    toggle.addEventListener('change', () => guard(api('/api/channels/settings', {
      name: channel.name, auto_record: toggle.checked,
    })));

    let action;
    if (channel.recording) {
      action = el('button', {
        class: 'stop', text: 'Stop',
        onclick: () => guard(api('/api/record/stop', { channel: channel.name })),
      });
    } else if (channel.armed) {
      action = el('button', {
        class: 'stop', text: 'Cancel',
        title: 'Stop waiting for this channel to go live',
        onclick: () => guard(api('/api/record/stop', { channel: channel.name })),
      });
    } else {
      action = el('button', {
        text: 'Record',
        title: 'Records now if the channel is live, otherwise as soon as it is',
        onclick: () => guard(
          api('/api/record/start', { channel: channel.name })
            .then(result => toast(result.state === 'armed'
              ? `${channel.name} is not live — will start recording when it is`
              : `Recording ${channel.name}`))),
      });
    }

    root.append(el('div', { class: 'channel' }, [
      el('span', { class: dotClass, title: statusText }),
      el('span', { class: 'name', text: channel.name }),
      el('span', { class: 'title', text: channel.title || statusText }),
      el('span', { class: 'auto' }, [toggle, el('label', { for: `auto-${channel.name}`, text: 'auto' })]),
      action,
      el('button', {
        class: 'danger', text: 'Remove',
        onclick: () => {
          // Removing a live channel used to hide the only Stop control.
          if (channel.recording) {
            return toast(`${channel.name} is recording. Stop it first.`, true);
          }
          if (!confirm(`Stop watching ${channel.name}? Recorded files are kept.`)) return;
          guard(api('/api/channels/remove', { name: channel.name }));
        },
      }),
    ]));
  }
}

/* -------------------------------------------------------------- live section */

function sessionExtent(session) {
  if (Number.isFinite(session.recorded_extent)) return session.recorded_extent;
  return session.chunks.reduce((max, chunk) => {
    const end = chunk.session_offset + (chunk.duration || 0);
    return Math.max(max, end);
  }, 0);
}

function liveElapsed(session) {
  return Math.max(0, (state.data?.now || Date.now() / 1000) - session.started_at);
}

function renderLive(data) {
  const root = $('#live');
  root.textContent = '';
  const sessions = data.sessions.filter(s => s.status === 'recording');
  if (!sessions.length) {
    root.append(el('div', { class: 'empty', text: 'Nothing recording right now.' }));
    return;
  }

  for (const session of sessions) {
    const isVod = session.source_kind === 'vod';
    const chunk = [...session.chunks].reverse().find(c => c.status === 'recording');
    const elapsed = liveElapsed(session);
    // The chunk clock runs from the chunk's own start, not the session's.
    const intoChunk = chunk
      ? Math.max(0, Number.isFinite(session.live_chunk_seconds)
        ? session.live_chunk_seconds
        : elapsed - chunk.session_offset)
      : 0;
    const chunkLength = (state.config?.recording?.chunk_seconds) || 7200;
    const progress = Math.min(100, (intoChunk / chunkLength) * 100);

    root.append(el('div', { class: 'live-session' }, [
      el('div', { class: 'live-head' }, [
        el('span', { class: 'dot rec' }),
        el('span', { class: 'who', text: session.channel }),
        isVod ? el('span', { class: 'badge on', title: session.source_url || 'VOD download', text: 'VOD' }) : null,
        el('span', { class: 'meta', text: `${isVod ? clock(sessionExtent(session)) + ' downloaded' : clock(elapsed) + ' elapsed · ' + clock(sessionExtent(session)) + ' recorded'} · ${session.chunks.length} chunk(s)` }),
        chunk ? el('span', { class: 'meta', text: `${chunk.label}: ${clock(intoChunk)} / ${clock(chunkLength)}` }) : null,
        (session.ad_events || []).length
          ? el('span', {
              class: 'meta',
              title: 'Informational only. Nothing is cut from the recording.',
              text: `· ${session.ad_events.length} ad event(s) noted`,
            })
          : null,
      ]),
      el('div', { class: 'bar' }, [el('span', { style: `width:${progress}%` })]),
      el('div', { class: 'snapshot' }, [
        // The channel row's Stop button is absent if the channel was removed
        // from the watch list, or was never on it (a direct API start), so the
        // live card carries its own.
        el('button', {
          class: 'stop', text: isVod ? 'Stop download' : 'Stop recording',
          onclick: () => guard(isVod
            ? api('/api/vod/stop', { session_id: session.session_id })
            : api('/api/record/stop', { channel: session.channel })),
        }),
        el('span', { class: 'label', text: session.directory }),
      ]),
      snapshotControls(session),
    ]));
  }
}

function snapshotControls(session) {
  // The cut is queued, not performed inline, so the answer names the job rather
  // than a finished file. Watch the Jobs panel; the file appears in the session's
  // outputs when it lands.
  const take = payload => guard(
    api('/api/snapshot', { session_id: session.session_id, transcribe: true, ...payload })
      .then(result => toast(`Cutting: ${result.label || 'snapshot queued'}`))
  );

  const startField = el('input', { type: 'text', placeholder: '00:00:00', id: `s-${session.session_id}` });
  const endField = el('input', { type: 'text', placeholder: clock(sessionExtent(session)), id: `e-${session.session_id}` });
  const nameField = el('input', {
    type: 'text', placeholder: 'name (optional)', maxlength: '120',
    title: 'Maximum 120 characters', id: `n-${session.session_id}`,
  });
  const precise = el('input', { type: 'checkbox', id: `p-${session.session_id}` });

  const quick = [5, 10, 20, 30].map(minutes => el('button', {
    text: `Last ${minutes}m`,
    onclick: () => take({ last_minutes: minutes, precise: precise.checked, name: nameField.value }),
  }));

  return el('div', { class: 'snapshot' }, [
    el('span', { class: 'label', text: 'Snapshot:' }),
    ...quick,
    el('span', { class: 'label', text: 'or range' }),
    startField,
    el('span', { class: 'label', text: '→' }),
    endField,
    el('button', {
      class: 'ghost', text: 'Cut',
      onclick: () => {
        const start = parseClock(startField.value);
        const end = parseClock(endField.value);
        if (start === null) return toast('Enter a start time as HH:MM:SS', true);
        take({ start, end: end === null ? undefined : end, precise: precise.checked, name: nameField.value });
      },
    }),
    nameField,
    el('span', { class: 'auto' }, [precise, el('label', { for: precise.id, text: 'frame-exact (slower)' })]),
  ]);
}

/* ---------------------------------------------------------------- sessions */

function statusChip(value) {
  return el('span', { class: `status ${value}`, text: value });
}

function renderSessions(data) {
  const root = $('#sessions');
  root.textContent = '';
  if (!data.sessions.length) {
    root.append(el('div', { class: 'empty', text: 'No sessions recorded yet.' }));
    return;
  }

  for (const session of data.sessions) {
    const open = state.expanded.has(session.session_id);
    const toggleSession = () => {
      if (open) state.expanded.delete(session.session_id);
      else state.expanded.add(session.session_id);
      localStorage.setItem('expanded', JSON.stringify([...state.expanded]));
      render();
      if (!open) loadOutputs(session.session_id);
    };
    const head = el('div', {
      class: 'session-head',
      role: 'button',
      tabindex: '0',
      'aria-expanded': open ? 'true' : 'false',
      onclick: toggleSession,
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();   // Space would otherwise scroll the page.
          toggleSession();
        }
      },
    }, [
      el('span', { class: session.status === 'recording' ? 'dot rec' : 'dot' }),
      el('span', { class: 'who', text: session.channel }),
      session.source_kind === 'vod'
        ? el('span', { class: 'badge on', title: session.source_url || 'Downloaded VOD', text: 'VOD' })
        : null,
      el('span', { class: 'when', text: when(session.started_at) }),
      statusChip(session.status),
      el('span', { class: 'when', text: `${session.chunks.length} chunk(s) · ${clock(sessionExtent(session))}` }),
      session.quality_selected
        ? el('span', {
            class: session.quality_warning ? 'status warn' : 'when',
            title: session.quality_warning
              || (session.quality_available || []).join(', '),
            text: session.quality_selected,
          })
        : null,
      el('span', { class: 'spacer' }),
      el('span', { class: 'when', text: open ? '▾' : '▸' }),
    ]);

    const node = el('div', { class: 'session' }, [head]);
    // Shown whether or not the session is expanded: a capture that came in
    // under the floor is the one thing the operator must not have to go
    // looking for.
    if (session.quality_warning) {
      node.append(el('div', { class: 'quality-warning' }, [
        el('strong', { text: 'Quality: ' }),
        el('span', { text: session.quality_warning }),
      ]));
    }
    if (session.error) {
      node.append(el('div', { class: 'session-error' }, [
        el('strong', { text: 'Session error: ' }),
        el('span', { text: session.error }),
      ]));
    }
    if (open) node.append(sessionBody(session));
    root.append(node);
  }
}

function sessionBody(session) {
  const rows = [...session.chunks]
    .sort((a, b) => a.index - b.index)
    .map(chunk => {
      const summaryAllowed = Boolean(
        chunk.summary_eligible && state.data?.capabilities?.summary_available,
      );
      const summaryReason = chunk.summary_eligible
        ? state.data?.capabilities?.summary_unavailable_reason
        : chunk.summary_eligibility_reason;
      return el('tr', {}, [
      el('td', { text: chunk.label }),
      el('td', { text: clock(chunk.session_offset) }),
      el('td', { text: clock(chunk.duration) }),
      el('td', { text: chunk.size_bytes ? bytes(chunk.size_bytes) : '—' }),
      el('td', { text: chunk.height ? `${chunk.width}x${chunk.height}` : '—' }),
      el('td', {}, [statusChip(chunk.status)]),
      el('td', {}, [statusChip(chunk.proxy_status)]),
      el('td', {}, [statusChip(chunk.transcript_status), chunk.word_count
        ? el('span', { class: 'when', text: ` ${chunk.word_count} words` }) : null]),
      el('td', {}, [statusChip(chunk.summary_status)]),
      el('td', {}, Object.keys(chunk.errors || {}).length
        ? [el('span', {
            class: 'status error',
            title: Object.entries(chunk.errors)
              .map(([name, text]) => `${name}: ${text}`).join('\n'),
            text: `${Object.keys(chunk.errors).length} error(s)`,
          })]
        : []),
      el('td', { class: 'actions' }, [
        el('button', {
          class: 'ghost', text: 'Re-transcribe',
          onclick: () => guard(api('/api/chunk/retranscribe', {
            session_id: session.session_id, chunk: chunk.label,
          }).then(() => toast(`Queued re-transcribe for ${chunk.label}`))),
        }),
        el('button', {
          class: 'ghost', text: 'Rundown',
          disabled: !summaryAllowed,
          title: summaryAllowed ? 'Generate this rundown' : (summaryReason || 'Rundown unavailable'),
          onclick: () => guard(api('/api/chunk/summarize', {
            session_id: session.session_id, chunk: chunk.label,
          }).then(() => toast(`Queued rundown for ${chunk.label}`))),
        }),
      ]),
      ]);
    });

  const table = el('table', {}, [
    el('thead', {}, [el('tr', {}, ['Chunk', 'Starts', 'Length', 'Size', 'Resolution', 'Master', 'Proxy', 'Transcript', 'Rundown', 'Problems', '']
      .map(label => el('th', { text: label })))]),
    el('tbody', {}, rows),
  ]);

  const body = el('div', { class: 'session-body' }, [table]);

  if ((session.ad_events || []).length) {
    // Deliberately worded as observations, not intervals: streamlink filters ad
    // segments before they reach the recording, so nothing was removed.
    body.append(el('div', {
      class: 'ads',
      text: 'Ad events noted (nothing was cut from the media): '
        + session.ad_events
            .map(e => `~${clock(e.approx_session_seconds || 0)} ${e.kind || 'ad'}`)
            .join(', '),
    }));
  }

  const outputs = state.outputs.get(session.session_id);
  if (outputs) {
    for (const group of outputs.groups) {
      body.append(el('div', {}, [
        el('div', { class: 'group-label', text: group.group }),
        el('div', { class: 'files' }, group.files.map(file => el('button', {
          class: 'file-chip',
          type: 'button',
          text: file.name,
          onclick: () => openFile(file),
        }))),
      ]));
    }
  }

  body.append(el('div', {}, [
    el('button', {
      class: 'ghost', text: 'Open session folder',
      onclick: () => guard(api('/api/reveal', { path: session.directory })),
    }),
  ]));

  return body;
}

async function loadOutputs(sessionId) {
  try {
    const payload = await api(`/api/outputs?session_id=${encodeURIComponent(sessionId)}`);
    state.outputs.set(sessionId, payload);
    render();
  } catch (err) {
    // A session whose folder was moved or cleared is not worth a toast on every poll.
    console.warn('outputs', err);
  }
}

/* -------------------------------------------------------------------- jobs */

function renderJobs(data) {
  const active = data.jobs.filter(job => job.status === 'queued' || job.status === 'running');
  $('#jobs-count').textContent = active.length ? `${active.length} active` : 'idle';

  const root = $('#jobs');
  root.textContent = '';
  if (!data.jobs.length) {
    root.append(el('div', { class: 'empty', text: 'Nothing queued.' }));
    return;
  }
  for (const job of data.jobs.slice(0, 25)) {
    root.append(el('div', { class: 'job' }, [
      statusChip(job.status === 'failed' ? 'error' : job.status),
      el('span', { class: 'label', text: job.label + (job.progress ? ` — ${job.progress}` : '') }),
      job.error ? el('span', { class: 'err', text: job.error.slice(0, 120) }) : null,
    ]));
  }
}

/* ------------------------------------------------------------------ header */

function renderHeader(data) {
  const caps = $('#capabilities');
  caps.textContent = '';
  const items = [
    ['Deepgram', data.capabilities.deepgram],
    ['Twitch token', data.capabilities.twitch_token],
    [data.capabilities.summary_provider === 'anthropic-api' ? 'Anthropic API' : 'claude -p',
      data.capabilities.summary_provider === 'anthropic-api'
        ? data.capabilities.anthropic_api
        : data.capabilities.claude_cli],
  ];
  for (const [label, on] of items) {
    caps.append(el('span', { class: `badge ${on ? 'on' : 'off'}`, title: on ? 'configured' : 'not configured', text: label }));
  }

  const disk = $('#disk');
  const free = data.disk.free_bytes;
  const floor = data.disk.floor_bytes;
  disk.textContent = `${bytes(free)} free`;
  disk.className = 'disk' + (free < floor ? ' critical' : (free < floor * 1.5 ? ' low' : ''));
  disk.title = `${data.disk.masters_root} — new chunks stop below ${bytes(floor)}`;
}

/* ------------------------------------------------------------------ modal */

let modalFile = null;
let previewToken = 0;

async function openFile(file) {
  modalFile = file;
  // Responses can arrive out of order; an older one must not overwrite a newer
  // selection.
  const token = ++previewToken;
  $('#modal-title').textContent = file.name;
  $('#modal-body').textContent = 'Loading…';
  $('#modal').hidden = false;
  try {
    const payload = await api(`/api/file?artifact_id=${encodeURIComponent(file.artifact_id)}`);
    if (token !== previewToken) return;
    $('#modal-body').textContent = payload.text;
  } catch (err) {
    if (token !== previewToken) return;
    $('#modal-body').textContent = `Could not read this file:\n${err.message}`;
  }
}

/* ----------------------------------------------------------------- settings */

const SETTINGS_SCHEMA = [
  { title: 'Secrets', fields: [
    { path: 'secrets.deepgram_api_key', label: 'Deepgram API key', type: 'password',
      desc: 'Required for transcription. Stored in config.json.' },
    { path: 'secrets.twitch_oauth_token', label: 'Twitch OAuth token', type: 'password',
      desc: 'Optional. With Twitch Turbo this is the reliable ad-free path.' },
    { path: 'secrets.anthropic_api_key', label: 'Anthropic API key', type: 'password',
      desc: 'Only needed if the summary provider is set to anthropic-api.' },
  ]},
  { title: 'Recording', fields: [
    { path: 'recording.chunk_seconds', label: 'Chunk length (seconds)', type: 'number' },
    { path: 'recording.quality', label: 'Stream quality', type: 'text',
      desc: 'streamlink rendition name. "best" takes the top of whatever ladder Twitch offers, which is not always 1080p. A fallback chain works too, e.g. "1080p60,best".' },
    { path: 'recording.min_height', label: 'Expected minimum height (px)', type: 'number',
      desc: 'Warn when a capture comes in below this. 0 disables the check.' },
    { path: 'recording.on_low_quality', label: 'If below that height', type: 'select',
      options: ['warn', 'refuse'],
      desc: 'warn records anyway and says so loudly; refuse stops the recording.' },
    { path: 'recording.free_space_floor_gb', label: 'Free space floor (GB)', type: 'number',
      desc: 'No new chunk opens below this.' },
    { path: 'recording.hard_reserve_gb', label: 'Hard reserve (GB)', type: 'number',
      desc: 'Crossing this aborts the session mid-chunk.' },
    { path: 'recording.keep_ts_after_remux', label: 'Keep the .ts working copy', type: 'checkbox' },
    { path: 'recording.twitch_low_latency', label: 'Twitch low latency', type: 'checkbox' },
    { path: 'recording.streamlink_no_config', label: 'Ignore your own streamlink config', type: 'checkbox',
      desc: 'Off by default. Turn it on for reproducible recordings; leave it off if you keep a token or plugin settings in streamlink’s own config.' },
  ]},
  { title: 'Proxies', fields: [
    { path: 'proxies.enabled', label: 'Generate proxies', type: 'checkbox' },
    { path: 'proxies.height', label: 'Proxy height (px)', type: 'number' },
    { path: 'proxies.encoder', label: 'Encoder', type: 'select', options: ['auto', 'h264_amf', 'h264_nvenc', 'h264_qsv', 'libx264'] },
    { path: 'proxies.quality', label: 'Quality (CRF / QP)', type: 'number' },
    { path: 'proxies.retention_days', label: 'Delete proxies after (days)', type: 'number' },
  ]},
  { title: 'Transcription', fields: [
    { path: 'transcription.enabled', label: 'Transcribe', type: 'checkbox' },
    { path: 'transcription.model', label: 'Deepgram model', type: 'text' },
    { path: 'transcription.language', label: 'Language', type: 'text' },
    { path: 'transcription.audio_stream', label: 'Audio track', type: 'text',
      desc: 'Use "auto", a zero-based audio ordinal (0, 1, ...), or a stream language tag such as en or es.' },
    { path: 'transcription.slice_seconds', label: 'Rolling slice (seconds)', type: 'number' },
    { path: 'transcription.filler_words', label: 'Transcribe "uh" and "um"', type: 'checkbox',
      desc: 'Keeps the transcript verbatim, so a cut made from the text lands where you expect and a filler can be selected and deleted on its own. Turn it off for cleaner reading copy. Premiere\'s automatic "delete all fillers" is not supported — it never recognised the tags.' },
    { path: 'transcription.stitch_chunk_boundaries', label: 'Repair words across chunk boundaries', type: 'checkbox',
      desc: 'Chunks are separate files, so a word spoken across the join is clipped in both. This re-transcribes a few seconds spanning the join — one extra short request per boundary.' },
  ]},
  { title: 'Snapshots', fields: [
    { path: 'snapshots.max_concurrent', label: 'Cuts at once (all sessions)', type: 'number',
      desc: 'Each cut is an ffmpeg run on the drive the recorder is writing to.' },
    { path: 'snapshots.max_per_session', label: 'Cuts at once (per session)', type: 'number' },
  ]},
  { title: 'Summary', fields: [
    { path: 'summary.enabled', label: 'Write rundowns', type: 'checkbox' },
    { path: 'summary.provider', label: 'Engine', type: 'select', options: ['claude-cli', 'anthropic-api', 'none'] },
    { path: 'summary.model', label: 'Model (API only)', type: 'text' },
    { path: 'summary.min_words', label: 'Minimum words for a rundown', type: 'number' },
    { path: 'summary.max_tokens', label: 'Maximum rundown output tokens', type: 'number' },
    { path: 'summary.max_retries', label: 'Rundown attempts', type: 'number',
      desc: 'How many times to ask the engine for one rundown before giving up. A rundown runs in the background and its engine can rate-limit — claude -p shares your Claude subscription quota — so a single transient refusal should not lose the rundown. All attempts share the one timeout below.' },
  ]},
  { title: 'Paths', fields: [
    { path: 'paths.masters_root', label: 'Masters folder', type: 'text',
      desc: 'Takes effect on restart. The session store, recovery and the '
            + 'file previews are all rooted at the folder this process started '
            + 'with, so changing it while running would split state across two '
            + 'locations.' },
    { path: 'paths.censor_master_list', label: 'Censor master list', type: 'text' },
  ]},
  { title: 'Network', fields: [
    { path: 'network.proxy', label: 'Proxy for streamlink', type: 'text',
      desc: 'Optional HTTP/SOCKS proxy for live capture, VOD download and live-status probes — e.g. socks5://127.0.0.1:1080 or http://user:pass@host:3128. This is how you reach Twitch from a region it has left (South Korea) and record source quality without a full VPN. Blank means a direct connection.' },
  ]},
  { title: 'Watcher', fields: [
    { path: 'watcher.enabled', label: 'Watch channels and auto-record', type: 'checkbox' },
    { path: 'watcher.check_seconds', label: 'Check every (seconds)', type: 'number' },
    { path: 'watcher.probe_timeout_seconds', label: 'Give up on a check after (seconds)', type: 'number' },
  ]},
];

const dig = (obj, path) => path.split('.').reduce((node, key) => (node ?? {})[key], obj);

function buildSettings(config) {
  const root = $('#settings-fields');
  root.textContent = '';
  for (const section of SETTINGS_SCHEMA) {
    root.append(el('div', { class: 'fieldset-title', text: section.title }));
    for (const field of section.fields) {
      const value = dig(config, field.path);
      let input;
      if (field.type === 'checkbox') {
        input = el('input', { type: 'checkbox', 'data-path': field.path, id: field.path });
        input.checked = Boolean(value);
        root.append(el('div', { class: 'field inline' }, [input, el('label', { for: field.path, text: field.label })]));
        if (field.desc) root.append(el('div', { class: 'desc', text: field.desc }));
        continue;
      }
      if (field.type === 'select') {
        input = el('select', { 'data-path': field.path, id: field.path },
          field.options.map(option => el('option', { value: option, text: option })));
        input.value = value ?? field.options[0];
      } else if (field.type === 'password') {
        // A stored secret round-trips as a sentinel; blank means "leave it alone",
        // because the browser sends empty for untouched password fields. Clearing
        // therefore has to be an explicit action.
        input = el('input', { type: 'password', 'data-path': field.path, id: field.path,
          placeholder: value === '__unchanged__' ? '•••••• stored' : 'not set' });
        input.value = '';
        if (value === '__unchanged__') {
          const clear = el('button', {
            class: 'ghost', type: 'button', text: 'Clear',
            onclick: () => { input.value = '__clear__'; input.type = 'text'; },
          });
          root.append(el('div', { class: 'field' }, [
            el('label', { for: field.path, text: field.label }),
            el('div', { class: 'snapshot' }, [input, clear]),
            field.desc ? el('div', { class: 'desc', text: field.desc }) : null,
          ]));
          continue;
        }
      } else {
        input = el('input', { type: field.type, 'data-path': field.path, id: field.path });
        input.value = value ?? '';
      }
      root.append(el('div', { class: 'field' }, [
        el('label', { for: field.path, text: field.label }),
        input,
        field.desc ? el('div', { class: 'desc', text: field.desc }) : null,
      ]));
    }
  }
}

function collectSettings() {
  const payload = {};
  for (const input of document.querySelectorAll('#settings-fields [data-path]')) {
    const path = input.getAttribute('data-path');
    let value;
    if (input.type === 'checkbox') value = input.checked;
    else if (input.type === 'number') value = input.value === '' ? null : Number(input.value);
    else if (input.type === 'password') {
      if (!input.value) continue;   // untouched: keep whatever is stored
      value = input.value;
    } else value = input.value;
    if (value === null) continue;

    const keys = path.split('.');
    let node = payload;
    for (const key of keys.slice(0, -1)) node = node[key] ??= {};
    node[keys.at(-1)] = value;
  }
  return payload;
}

/* ------------------------------------------------------------------- render */

function render() {
  const data = state.data;
  if (!data) return;
  renderHeader(data);
  // Re-rendering a section discards what the user is typing in it, so only the
  // section holding the focused field is held back. Previously any focused
  // input froze the whole live view, including recording status.
  const active = document.activeElement;
  const frozen = sel => isEditing() && active && active.closest(sel);
  if (!frozen('#channels-panel')) renderChannels(data);
  if (!frozen('#live-panel')) renderLive(data);
  if (!frozen('#sessions-panel')) renderSessions(data);
  renderJobs(data);
}

async function refresh() {
  if (state.polling) return;      // a slow response must not queue another
  state.polling = true;
  try {
    state.data = await api('/api/state');
    state.lastRefresh = Date.now();
    state.refreshError = null;
    renderConnection();
    render();
    for (const id of state.expanded) {
      if (!state.outputs.has(id)) loadOutputs(id);
    }
  } catch (err) {
    state.refreshError = err.message || 'Could not reach the dashboard server';
    renderConnection();
    console.warn('state', err);
  } finally {
    state.polling = false;
  }
}

/* --------------------------------------------------------------------- wire */

$('#add-channel').addEventListener('submit', event => {
  event.preventDefault();
  const input = $('#channel-input');
  const name = input.value.trim();
  if (!name) return;
  guard(api('/api/channels/add', { name }).then(() => { input.value = ''; }));
});

$('#add-vod').addEventListener('submit', event => {
  event.preventDefault();
  const input = $('#vod-input');
  const url = input.value.trim();
  if (!url) return;
  const start = parseClock($('#vod-start').value);
  const duration = parseClock($('#vod-duration').value);
  const payload = { url };
  if (start !== null) payload.start = start;
  if (duration !== null) payload.duration = duration;
  guard(api('/api/vod/download', payload).then(result => {
    input.value = ''; $('#vod-start').value = ''; $('#vod-duration').value = '';
    toast(`Downloading VOD → ${result.channel || 'session started'}`);
  }));
});

$('#settings-toggle').addEventListener('click', async () => {
  try {
    state.config = await api('/api/config');
    buildSettings(state.config);
    $('#settings').hidden = false;
  } catch (err) {
    toast(err.message, true);
  }
});

$('#settings-close').addEventListener('click', () => { $('#settings').hidden = true; });

$('#settings-save').addEventListener('click', async () => {
  const status = $('#settings-status');
  status.textContent = 'Saving…';
  try {
    const payload = await api('/api/config', collectSettings());
    state.config = payload.config;
    buildSettings(state.config);
    status.textContent = 'Saved.';
    toast('Settings saved');
    refresh();
  } catch (err) {
    status.textContent = '';
    toast(err.message, true);
  }
});

$('#modal-close').addEventListener('click', () => { $('#modal').hidden = true; });
$('#modal').addEventListener('click', event => {
  if (event.target.id === 'modal') $('#modal').hidden = true;
});
$('#modal-reveal').addEventListener('click', () => {
  if (modalFile) guard(api('/api/reveal', { artifact_id: modalFile.artifact_id }));
});

document.addEventListener('click', event => {
  const target = event.target.closest('[data-toggle]');
  if (!target) return;
  const panel = document.getElementById(target.getAttribute('data-toggle'));
  panel.classList.toggle('collapsed');
  target.textContent = panel.classList.contains('collapsed') ? 'Show' : 'Hide';
});

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  $('#modal').hidden = true;
  $('#settings').hidden = true;
});

async function boot() {
  renderConnection();
  try {
    state.config = await api('/api/config');
    const seconds = Number(state.config?.dashboard?.poll_seconds);
    if (Number.isFinite(seconds) && seconds > 0) state.pollMs = seconds * 1000;
  } catch {
    // Fall back to the built-in poll interval; the state endpoint is what matters.
  }
  await refresh();
  setInterval(refresh, state.pollMs);
  // Expanded sessions get their file lists refreshed less often than the state poll.
  setInterval(() => { for (const id of state.expanded) loadOutputs(id); }, 15000);
}

boot();
