const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.join(__dirname, '../frontend/static/app.js');
const source = fs.readFileSync(appPath, 'utf8');
const start = source.indexOf('// Manage Playlists functionality');
const end = source.indexOf('// System Check functionality', start);
assert.notEqual(start, -1, 'manage-playlists code must exist');
assert.notEqual(end, -1, 'manage-playlists section must have a closing marker');
const managePlaylistsCode = source.slice(start, end);

function makeResponse(data, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        async json() { return data; }
    };
}

function createHarness(initialPlaylists, failIds = [], fallbackIds = []) {
    let playlists = structuredClone(initialPlaylists);
    const postOrder = [];
    const toasts = [];
    const button = { disabled: false, innerHTML: 'Refresh all' };
    const loading = { classList: { add() {}, remove() {} } };
    const container = { innerHTML: '' };
    const elements = {
        'refresh-all-playlists-btn': button,
        'playlists-loading': loading,
        'playlists-container': container
    };
    let activePosts = 0;
    let maxActivePosts = 0;

    const context = {
        document: { getElementById: id => elements[id] || null },
        async fetch(url, options = {}) {
            if (url === '/api/playlists' && !options.method) {
                return makeResponse(structuredClone(playlists));
            }
            const match = url.match(/^\/api\/playlists\/([^/]+)\/refresh$/);
            if (!match || options.method !== 'POST') {
                throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
            }
            const id = decodeURIComponent(match[1]);
            postOrder.push(id);
            activePosts += 1;
            maxActivePosts = Math.max(maxActivePosts, activePosts);
            await new Promise(resolve => setTimeout(resolve, 1));
            activePosts -= 1;
            if (failIds.includes(id)) {
                return makeResponse({ detail: 'simulated refresh failure' }, 500);
            }
            playlists = playlists.map(item => item.navidrome_playlist_id === id
                ? {
                    ...item,
                    last_refreshed: `updated-${id}`,
                    reasoning: fallbackIds.includes(id) ? 'Fallback curation: simulated AI outage' : 'AI curation applied',
                    track_count: item.track_count || 25
                }
                : item);
            return makeResponse({ message: 'refreshed' });
        },
        showToast(type, message, duration) { toasts.push({ type, message, duration }); },
        updatePlaylistCount() {},
        formatFriendlyDate(value) { return value || 'date'; },
        console: { error() {} },
        setTimeout(callback) { callback(); return 0; },
        clearTimeout() {},
        Promise,
        Set,
        Date,
        encodeURIComponent,
        decodeURIComponent
    };

    vm.createContext(context);
    vm.runInContext(managePlaylistsCode, context);
    return { context, postOrder, toasts, button, getMaxActivePosts: () => maxActivePosts };
}

async function testRefreshesScheduledPlaylistsSequentiallyWithPerPlaylistProgress() {
    const playlists = [
        { id: 1, navidrome_playlist_id: 'house-1', playlist_name: 'House', refresh_frequency: 'weekly', last_refreshed: 'old', reasoning: 'Fallback', track_count: 25 },
        { id: 2, navidrome_playlist_id: 'manual-1', playlist_name: 'Manual', refresh_frequency: 'manually', last_refreshed: null, reasoning: '' },
        { id: 3, navidrome_playlist_id: 'artist-1', playlist_name: 'This Is Artist', refresh_frequency: 'monthly', last_refreshed: null, reasoning: '' }
    ];
    const h = createHarness(playlists);
    await h.context.refreshAllPlaylists();

    assert.deepEqual(h.postOrder, ['house-1', 'artist-1']);
    assert.equal(h.getMaxActivePosts(), 1, 'refresh requests must not overlap');
    const progress = h.toasts.filter(t => t.type === 'loading').map(t => t.message);
    assert.deepEqual(progress, [
        'Refreshing playlist 1/2: House',
        'Refreshing playlist 2/2: This Is Artist'
    ]);
    assert.equal(h.toasts.at(-1).type, 'success');
    assert.match(h.toasts.at(-1).message, /All 2 scheduled playlists refreshed/);
    assert.equal(h.button.disabled, false);
}

async function testContinuesAfterOnePlaylistFailureAndReportsIt() {
    const playlists = [
        { id: 1, navidrome_playlist_id: 'first', playlist_name: 'First', refresh_frequency: 'weekly', last_refreshed: null, reasoning: '' },
        { id: 2, navidrome_playlist_id: 'broken', playlist_name: 'Broken', refresh_frequency: 'weekly', last_refreshed: null, reasoning: '' },
        { id: 3, navidrome_playlist_id: 'last', playlist_name: 'Last', refresh_frequency: 'monthly', last_refreshed: null, reasoning: '' }
    ];
    const h = createHarness(playlists, ['broken']);
    await h.context.refreshAllPlaylists();

    assert.deepEqual(h.postOrder, ['first', 'broken', 'last']);
    assert.equal(h.toasts.at(-1).type, 'error');
    assert.match(h.toasts.at(-1).message, /Broken/);
    assert.equal(h.button.disabled, false);
}

async function testReportsSilentBackendFallbackAsFailure() {
    const playlists = [
        { id: 1, navidrome_playlist_id: 'fallback', playlist_name: 'Fallback playlist', refresh_frequency: 'weekly', last_refreshed: null, reasoning: '', track_count: 25 }
    ];
    const h = createHarness(playlists, [], ['fallback']);
    await h.context.refreshAllPlaylists();

    assert.deepEqual(h.postOrder, ['fallback']);
    assert.equal(h.toasts.at(-1).type, 'error');
    assert.match(h.toasts.at(-1).message, /fallback instead of AI curation/i);
}

(async () => {
    await testRefreshesScheduledPlaylistsSequentiallyWithPerPlaylistProgress();
    await testContinuesAfterOnePlaylistFailureAndReportsIt();
    await testReportsSilentBackendFallbackAsFailure();
    console.log('PASS: refresh-all is sequential, reports each playlist, and recovers from an individual failure.');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
