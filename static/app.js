'use strict';
const $ = id => document.getElementById(id);
let state = null,
    schemaKey = '',
    busy = false,
    page = 'live',
    kind = 'all',
    offset = 0,
    viewed = null,
    toastTimer, connected = false,
    polling = false,
    libraryRequest = 0;
const bytes = n => n >= 1e9 ? `${(n/1e9).toFixed(1)} GB` : `${(n/1e6).toFixed(1)} MB`;
const duration = n => `${Math.floor(n/60).toString().padStart(2,'0')}:${Math.floor(n%60).toString().padStart(2,'0')}`;
const date = n => new Date(n).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
});

function tickAthensClock() {
    $('athens-clock').textContent = new Date().toLocaleString('en-GB', {
        timeZone: 'Europe/Athens',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }) + ' Athens';
}
setInterval(tickAthensClock, 1000);
tickAthensClock();

function toast(message, error = false) {
    if (error && $('viewer').open) $('viewer-info').textContent = message;
    $('toast').textContent = message;
    $('toast').classList.toggle('error', error);
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').hidden = true, error ? 8000 : 4000);
}
async function api(path, data, method = 'POST') {
    const response = await fetch(path, data === undefined ? {} : {
        method,
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    });
    const result = await response.json();
    if (response.status === 401) {
        if (!$('login').open) $('login').showModal();
        connected = false;
    }
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
}
async function action(task) {
    if (busy) return;
    busy = true;
    updateButtons();
    try {
        await task();
    } catch (e) {
        schemaKey = '';
        toast(e.message, true);
    } finally {
        busy = false;
        await poll();
        updateButtons();
    }
}

function updateButtons() {
    $('capture').disabled = busy || !connected || !state?.ready;
    $('record').disabled = busy || !connected || !state?.ready;
    $('global-stop').disabled = busy || !connected;
    $('apply-config').disabled = busy || !!state?.recording;
    $('capture-title').textContent = busy ? 'Working…' : state?.recording ? 'Recording' : 'Ready';
}

function grid(target, scores) {
    target.replaceChildren();
    const max = Math.max(...scores);
    scores.forEach(score => {
        const cell = document.createElement('div');
        cell.className = 'focus-cell' + (score === max ? ' best' : '');
        const label = document.createElement('span');
        label.textContent = score.toFixed(0);
        cell.append(label);
        target.append(cell);
    });
}

function preview() {
    if (page === 'live' && !document.hidden && connected && state?.ready && !$('feed').hasAttribute('src')) $('feed').src = '/stream.mjpg';
    else if (page !== 'live' || document.hidden || !connected || !state?.ready) $('feed').removeAttribute('src');
}
async function poll() {
    if (document.hidden || $('login').open || polling) return;
    polling = true;
    try {
        state = await api('/api/status');
        connected = true;
        checkServerTime();
        $('connection').textContent = state.ready ? '● Connected to Pi' : 'Camera unavailable';
        $('connection').classList.toggle('offline', !state.ready);
        $('camera-name').textContent = (state.properties.Model || 'Camera').toUpperCase();
        $('sensor-label').textContent = state.properties.PixelArraySize?.join(' × ') || '';
        $('free-space').textContent = bytes(state.free_bytes) + ' free';
        $('camera-offline').hidden = state.ready;
        $('camera-error').textContent = state.error || 'Check your camera connection.';
        const stale = state.ready && state.frame_age > 5;
        $('notice').hidden = !state.error && !stale;
        $('notice').textContent = state.error || (stale ? 'Preview has not updated for more than 5 seconds. Check long-exposure settings or reconnect the camera.' : '');
        $('live-badge').textContent = stale ? '○ WAITING FOR FRAME' : '● LIVE';
        $('shutter').textContent = state.metadata.ExposureTime ? `${(state.metadata.ExposureTime/1000).toFixed(1)} ms` : '—';
        $('gain').textContent = state.metadata.AnalogueGain ? `${state.metadata.AnalogueGain.toFixed(1)}×` : '—';
        $('temperature').textContent = state.metadata.ColourTemperature ? `${state.metadata.ColourTemperature} K` : '—';
        $('actual-fps').textContent = state.metadata.FrameDuration ? `${(1e6/state.metadata.FrameDuration).toFixed(1)} fps` : '—';
        $('record-badge').hidden = !state.recording;
        $('timer').textContent = duration(state.elapsed);
        $('global-stop').hidden = !state.recording;
        $('global-timer').textContent = duration(state.elapsed);
        $('record').classList.toggle('recording', !!state.recording);
        $('record-label').textContent = state.recording ? 'Stop recording' : 'Record video';
        $('capture-detail').textContent = state.recording ? `${state.profile} snapshot · no interruption` : 'Full-resolution JPEG · saved locally';
        $('live-grid').hidden = !state.focus_enabled;
        $('focus-note').hidden = !state.focus_enabled;
        $('focus-toggle').checked = state.focus_enabled;
        if (state.focus_enabled) grid($('live-grid'), state.focus);
        const key = JSON.stringify([state.index, state.profile, state.fps, state.rotation, state.controls, state.applied]);
        if (schemaKey !== key) {
            schemaKey = key;
            buildControls();
        }
        preview();
        updateButtons();
    } catch (e) {
        connected = false;
        $('connection').textContent = 'Disconnected';
        $('connection').classList.add('offline');
        $('notice').hidden = false;
        $('notice').textContent = 'Connection lost. Recording on the Pi may still be running. Reconnecting…';
        preview();
        updateButtons();
    } finally {
        polling = false;
    }
}
const groups = {
    exposure: [
        ['AeEnable', 'Auto exposure'],
        ['ExposureTimeMode', 'Shutter mode'],
        ['AnalogueGainMode', 'Gain mode'],
        ['ExposureValue', 'Exposure compensation'],
        ['ExposureTime', 'Shutter · µs'],
        ['AnalogueGain', 'Analogue gain']
    ],
    colour: [
        ['AwbEnable', 'Auto white balance'],
        ['AwbMode', 'White balance preset'],
        ['Brightness', 'Brightness'],
        ['Contrast', 'Contrast'],
        ['Saturation', 'Saturation'],
        ['Sharpness', 'Sharpness']
    ],
    focus: [
        ['AfMode', 'Focus mode'],
        ['LensPosition', 'Lens position · dioptres'],
        ['AfRange', 'Focus range'],
        ['AfSpeed', 'Focus speed']
    ]
};

function current(name, spec) {
    if (spec.type === 'Bool') return state.applied[name] ?? spec.default ?? true;
    return state.applied[name] ?? state.metadata[name] ?? spec.default ?? spec.min;
}

function buildControls() {
    $('camera-select').replaceChildren();
    state.cameras.forEach(c => $('camera-select').add(new Option(`${c.Model.toUpperCase()} · camera ${c.Num}`, c.Num)));
    $('camera-select').value = state.index;
    $('profile').value = state.profile;
    $('fps').value = state.fps;
    $('rotation').value = state.rotation;
    for (const [group, names] of Object.entries(groups)) {
        const container = $(group + '-controls');
        container.replaceChildren();
        for (const [name, title] of names) {
            const spec = state.controls[name];
            if (!spec) continue;
            if (name === 'AeEnable' && state.controls.ExposureTimeMode && state.controls.AnalogueGainMode) continue;
            const row = document.createElement('div');
            row.className = 'control-row';
            const label = document.createElement('label');
            label.className = 'control-label';
            label.htmlFor = 'control-' + name;
            label.textContent = title;
            const input = document.createElement(Object.keys(spec.options).length ? 'select' : 'input');
            input.id = 'control-' + name;
            const value = current(name, spec);
            if (input.tagName === 'SELECT') {
                for (const [label, value] of Object.entries(spec.options))
                    if (value >= spec.min && value <= spec.max) input.add(new Option(label, value));
                input.value = value;
            } else if (spec.type === 'Bool') {
                input.type = 'checkbox';
                input.checked = value ?? true;
                label.className = 'switch';
            } else {
                input.type = ['ExposureTime', 'AnalogueGain', 'LensPosition'].includes(name) ? 'number' : 'range';
                input.min = spec.min;
                input.max = spec.max;
                input.step = spec.type === 'Float' ? '0.1' : '1';
                input.value = value;
                const output = document.createElement('output');
                output.textContent = input.value;
                label.append(output);
                input.oninput = () => output.textContent = input.value;
            }
            input.onchange = () => action(async () => {
                const v = input.type === 'checkbox' ? input.checked : Number(input.value);
                const values = {
                    [name]: v
                };
                if (name === 'AeEnable') {
                    for (const mode of ['ExposureTimeMode', 'AnalogueGainMode'])
                        if (state.controls[mode]) values[mode] = v ? 0 : 1;
                }
                if (name === 'ExposureTime' || name === 'AnalogueGain') {
                    const mode = name + 'Mode';
                    if (state.controls[mode]) values[mode] = 1;
                    else values.AeEnable = false;
                }
                if (name === 'LensPosition') values.AfMode = 0;
                if (name === 'AwbMode') values.AwbEnable = true;
                await api('/api/controls', values);
                toast(`${title} updated`);
            });
            if (spec.type === 'Bool') {
                label.append(input);
                row.append(label);
            } else row.append(label, input);
            container.append(row);
        }
    }
    if (!state.controls.AfMode) {
        const note = document.createElement('p');
        note.className = 'help';
        note.textContent = 'Fixed-focus lens. No electronic focus control is advertised by this camera. Use the sharpness grid to inspect local detail.';
        $('focus-controls').append(note);
    } else {
        const button = document.createElement('button');
        button.textContent = 'Autofocus once';
        button.className = 'wide';
        button.onclick = () => action(async () => {
            await api('/api/controls', {
                AfMode: 1,
                AfTrigger: 0
            });
            toast('Autofocus triggered');
            schemaKey = '';
        });
        $('focus-controls').append(button);
    }
    const advancedName = $('advanced-select').value;
    $('advanced-select').replaceChildren();
    Object.keys(state.controls).sort().forEach(name => $('advanced-select').add(new Option(name, name)));
    if (state.controls[advancedName]) $('advanced-select').value = advancedName;
    advanced();
    $('sensor-info').textContent = JSON.stringify({
        properties: state.properties,
        modes: state.modes
    }, null, 2);
}

function advanced() {
    const name = $('advanced-select').value,
        s = state?.controls[name];
    if (!s) return;
    $('advanced-help').textContent = `${s.type}${s.size?' · '+s.size+' values':''} · range ${JSON.stringify(s.min)} to ${JSON.stringify(s.max)}${Object.keys(s.options).length?' · '+JSON.stringify(s.options):''}`;
    let value = current(name, s);
    if (s.size && !Array.isArray(value)) value = Array(s.size).fill(value);
    $('advanced-value').value = JSON.stringify(value);
}

function showPage(next) {
    page = next;
    $('live-page').hidden = next !== 'live';
    $('library-page').hidden = next !== 'library';
    for (const name of ['live', 'library']) {
        $(name + '-tab').classList.toggle('active', name === next);
        if (name === next) $(name + '-tab').setAttribute('aria-current', 'page');
        else $(name + '-tab').removeAttribute('aria-current');
    }
    preview();
    if (next === 'library') loadLibrary();
}

function card(item) {
    const button = document.createElement('button');
    button.className = 'media-card';
    button.setAttribute('aria-label', `View ${item.kind} ${date(item.created)}`);
    const image = document.createElement('img');
    image.src = `/media/${item.id}/thumb`;
    image.loading = 'lazy';
    image.alt = '';
    const info = document.createElement('div');
    info.className = 'card-info';
    const title = document.createElement('strong');
    title.textContent = item.kind === 'video' ? `Video · ${duration(item.duration)}` : 'Photo';
    const time = document.createElement('span');
    time.textContent = date(item.created);
    info.append(title, time);
    button.append(image, info);
    button.onclick = () => openViewer(item);
    return button;
}
async function recent() {
    try {
        const result = await api('/api/media');
        $('library-count').textContent = result.total;
        if (result.items.length) {
            $('recent').replaceChildren(...result.items.slice(0, 4).map(card));
        } else {
            const empty = document.createElement('p');
            empty.className = 'empty-inline';
            empty.textContent = 'No captures yet.';
            $('recent').replaceChildren(empty);
        }
    } catch (e) {
        toast(e.message, true);
    }
}
async function loadLibrary(more = false) {
    const version = ++libraryRequest;
    try {
        if (!more) {
            offset = 0;
            $('library-grid').replaceChildren();
        }
        const result = await api(`/api/media?kind=${kind}&offset=${offset}`);
        if (version !== libraryRequest) return;
        $('library-grid').append(...result.items.map(card));
        offset += result.items.length;
        $('results-count').textContent = `${result.total} capture${result.total===1?'':'s'}`;
        $('library-empty').hidden = result.total !== 0;
        $('load-more').hidden = offset >= result.total;
    } catch (e) {
        toast(e.message, true);
    }
}
async function openViewer(item) {
    viewed = item;
    $('viewer-title').textContent = date(item.created);
    $('viewer-kind').textContent = `${item.camera} / ${item.kind==='video'?'MOTION':'STILL'}`;
    $('viewer-image').hidden = item.kind === 'video';
    $('viewer-video').hidden = item.kind !== 'video';
    $('load-original').hidden = item.kind === 'video';
    $('load-original').disabled = false;
    $('load-original').textContent = 'Load full resolution';
    $('viewer-focus').checked = false;
    $('viewer-grid').hidden = true;
    $('download').href = `/media/${item.id}/original?download=1`;
    $('download-metadata').href = `/media/${item.id}/metadata?download=1`;
    $('viewer-info').textContent = `${item.width} × ${item.height} · ${bytes(item.bytes)} · ${item.capture_mode || 'H.264 / MP4'} · ${item.id}`;
    if (item.kind === 'video') {
        $('viewer-image').removeAttribute('src');
        $('viewer-video').poster = `/media/${item.id}/thumb`;
        $('viewer-video').src = `/media/${item.id}/original`;
    } else {
        $('viewer-video').removeAttribute('src');
        $('viewer-image').src = `/media/${item.id}/thumb`;
        const preload = new Image();
        preload.onload = () => {
            if (viewed?.id === item.id && !$('load-original').disabled) $('viewer-image').src = preload.src;
        };
        preload.src = `/media/${item.id}/preview`;
    }
    $('viewer').showModal();
}

function videoFocus() {
    if (!$('viewer').open || !$('viewer-focus').checked || viewed?.kind !== 'video') return;
    const video = $('viewer-video');
    if (video.readyState < 2) return;
    const canvas = document.createElement('canvas');
    canvas.width = 480;
    canvas.height = Math.round(480 * video.videoHeight / video.videoWidth);
    const ctx = canvas.getContext('2d', {
        willReadFrequently: true
    });
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const {
        data
    } = ctx.getImageData(0, 0, canvas.width, canvas.height), w = canvas.width, h = canvas.height;
    const lum = new Float32Array(w * h);
    for (let i = 0; i < lum.length; i++) lum[i] = .299 * data[i * 4] + .587 * data[i * 4 + 1] + .114 * data[i * 4 + 2];
    const sums = Array(9).fill(0),
        squares = Array(9).fill(0),
        counts = Array(9).fill(0);
    for (let y = 1; y < h - 1; y++)
        for (let x = 1; x < w - 1; x++) {
            const i = y * w + x,
                v = lum[i - 1] + lum[i + 1] + lum[i - w] + lum[i + w] - 4 * lum[i],
                cell = Math.min(2, Math.floor(y / h * 3)) * 3 + Math.min(2, Math.floor(x / w * 3));
            sums[cell] += v;
            squares[cell] += v * v;
            counts[cell]++;
        }
    grid($('viewer-grid'), sums.map((sum, i) => Math.max(0, squares[i] / counts[i] - (sum / counts[i]) ** 2)));
}
$('live-tab').onclick = () => showPage('live');
$('library-tab').onclick = $('see-library').onclick = () => showPage('library');
$('empty-live').onclick = () => showPage('live');
$('capture').onclick = () => action(async () => {
    const item = await api('/api/capture', {});
    toast(`Photo saved · ${item.width} × ${item.height}`);
    await recent();
});
$('record').onclick = () => action(async () => {
    const stopping = !!state.recording;
    await api('/api/record/' + (stopping ? 'stop' : 'start'), {});
    toast(stopping ? 'Video saved to your library' : 'Recording on the Pi. Keep this page handy to stop.');
    await recent();
});
$('global-stop').onclick = () => action(async () => {
    await api('/api/record/stop', {});
    toast('Video saved to your library');
    await recent();
    if (page === 'library') await loadLibrary();
});
$('focus-toggle').onchange = () => action(async () => {
    await api('/api/focus', {
        enabled: $('focus-toggle').checked
    });
});
$('configure-form').onsubmit = e => {
    e.preventDefault();
    action(async () => {
        await api('/api/configure', {
            index: Number($('camera-select').value),
            profile: $('profile').value,
            fps: Number($('fps').value),
            rotation: Number($('rotation').value)
        });
        schemaKey = '';
        toast('Video settings applied');
    });
};
$('retry').onclick = () => action(async () => {
    await api('/api/configure', {});
    schemaKey = '';
});
$('advanced-select').onchange = advanced;
$('apply-advanced').onclick = () => action(async () => {
    await api('/api/controls', {
        [$('advanced-select').value]: JSON.parse($('advanced-value').value)
    });
    schemaKey = '';
    toast('Control applied');
});
$('fullscreen').onclick = () => {
    const promise = document.fullscreenElement ? document.exitFullscreen() : $('viewfinder').requestFullscreen?.();
    promise?.catch(e => toast(e.message, true));
};
$('refresh-library').onclick = () => loadLibrary();
$('load-more').onclick = () => loadLibrary(true);
document.querySelectorAll('[data-kind]').forEach(button => button.onclick = () => {
    kind = button.dataset.kind;
    document.querySelectorAll('[data-kind]').forEach(b => b.classList.toggle('selected', b === button));
    loadLibrary();
});
$('close-viewer').onclick = () => $('viewer').close();
$('viewer').onclose = () => {
    $('viewer-video').pause();
    $('viewer-video').removeAttribute('src');
    $('viewer-video').load();
    viewed = null;
};
$('viewer-focus').onchange = () => {
    $('viewer-grid').hidden = !$('viewer-focus').checked;
    if (viewed?.kind === 'still') grid($('viewer-grid'), viewed.focus || []);
    else videoFocus();
};
$('viewer-video').addEventListener('seeked', videoFocus);
$('load-original').onclick = () => {
    $('viewer-image').onload = () => {
        if ($('viewer-image').src.endsWith('/original')) $('load-original').textContent = 'Full resolution loaded';
    };
    $('viewer-image').onerror = () => {
        toast('Could not load the original. Try downloading it.', true);
        $('load-original').disabled = false;
    };
    $('viewer-image').src = `/media/${viewed.id}/original`;
    $('load-original').textContent = 'Loading original…';
    $('load-original').disabled = true;
};
$('delete-media').onclick = () => {
    if (!viewed || !confirm('Permanently delete this capture and its metadata from the Pi? Download it first if you want to keep it.')) return;
    action(async () => {
        await api(`/api/media/${viewed.id}`, {}, 'DELETE');
        $('viewer').close();
        await recent();
        await loadLibrary();
        toast('Capture deleted');
    });
};
$('login').addEventListener('cancel', e => e.preventDefault());
$('login-form').onsubmit = async e => {
    e.preventDefault();
    try {
        await api('/api/login', {
            password: $('password').value
        });
        $('password').value = '';
        $('login').close();
        await poll();
        await recent();
    } catch (e) {
        $('login-error').textContent = e.message;
    }
};
document.addEventListener('visibilitychange', () => {
    preview();
    if (!document.hidden) poll();
});
$('feed').onerror = () => {
    $('feed').removeAttribute('src');
    $('live-badge').textContent = '○ RECONNECTING';
};
setInterval(poll, 1200);
setInterval(videoFocus, 500);

async function checkServerTime() {
    try {
        const t = await api('/api/time');
        const effective = t.epoch + (t.offset || 0);
        const drift = Math.abs(Date.now() / 1000 - effective);
        $('athens-time').textContent = new Date(t.athens).toLocaleString('en-GB', {
            timeZone: 'Europe/Athens', year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
        $('server-time').textContent = new Date(t.utc).toLocaleString('en-GB', {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
        if (drift > 30) {
            $('time-drift').hidden = false;
            $('time-drift').textContent = `Your browser and the applied Pi time differ by ${Math.round(drift)} seconds. Use "Apply time" below to correct.`;
        } else {
            $('time-drift').hidden = true;
        }
        const athensDate = new Date(t.athens);
        $('set-time-value').value = athensDate.toISOString().slice(0, 16);
    } catch (e) {}
}

$('set-time').onclick = () => action(async () => {
    const raw = $('set-time-value').value;
    if (!raw) { toast('Select a date and time first.', true); return; }
    await api('/api/time', { time: raw });
    toast('Time offset applied to Camera Deck');
    await checkServerTime();
});

(async () => {
    await poll();
    if (connected) await recent();
})();