/**
 * Tests for `worker/index.js`.
 *
 * Run with `node --test worker/test_worker.mjs`.
 *
 * `caches.default` and `fetch` are the only two things the worker asks of its
 * runtime, so both are replaced here with in-memory stand-ins: a Map for the
 * edge cache and a URL router for upstream. Nothing reaches GitHub, and every
 * test can assert how many upstream requests a route actually made — which is
 * how the caching claims in the worker get proven rather than assumed.
 */
import test from "node:test";
import assert from "node:assert/strict";

import worker from "./index.js";

const ENV = { GITHUB_USER: "miaoermua", GITHUB_REPO: "pica-pkg-repo" };
const ORIGIN = "https://pica.example.workers.dev";
const RAW = "https://raw.githubusercontent.com/miaoermua/pica-pkg-repo/main/r/";
const RELEASES = "https://github.com/miaoermua/pica-pkg-repo/releases/download/";
const ASSET = "demo-1.0-1-amd64-x86_64-apk.pkg.tar.gz";
const SHA = "a".repeat(64);

const DESCRIPTOR = {
  schema: 1,
  name: "Pica",
  managers: {
    apk: { path: "apk/index.json" },
    pica: { path: "pica/index.json" },
  },
};

const APK_INDEX = {
  schema: 1,
  mgr: "apk",
  packages: [
    {
      pkgname: "demo",
      pkgver: "1.0",
      pkgrel: "1",
      pkgmgr: "apk",
      platform: "amd64",
      arch: "x86_64",
      filename: ASSET,
      sha256: SHA,
      download_url: `${RELEASES}latest/${ASSET}`,
    },
  ],
};

function repoIndex(overrides = {}) {
  return {
    schema: 2,
    packages: [{ ...APK_INDEX.packages[0], ...overrides }],
  };
}

class MemoryCache {
  constructor() {
    this.entries = new Map();
  }

  async match(request) {
    const hit = this.entries.get(request.url);
    return hit ? hit.clone() : undefined;
  }

  async put(request, response) {
    this.entries.set(request.url, response.clone());
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/**
 * Install a cache and an upstream router for one test. `overrides` maps an
 * exact upstream URL to a function returning that response, so a test can make
 * one endpoint fail without disturbing the others. `origin` is the origin the
 * request arrives on, which is what a rewritten `download_url` is built from.
 */
function setup({ overrides = {}, repo = null, cache = new MemoryCache(), origin = ORIGIN } = {}) {
  const calls = [];

  globalThis.caches = { default: cache };
  globalThis.fetch = async (input) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push(url);
    if (overrides[url]) return overrides[url]();
    if (url === `${RAW}index.json`) return json(DESCRIPTOR);
    if (url === `${RAW}repo.json`) return json(repo || repoIndex());
    if (url === `${RAW}apk/index.json`) return json(APK_INDEX);
    if (url === `${RAW}pica/index.json`) return json({ schema: 1, mgr: "pica", packages: [] });
    if (url.startsWith(RELEASES)) {
      return new Response("artifact-bytes", {
        status: 200,
        headers: { "content-type": "application/octet-stream", etag: '"abc"' },
      });
    }
    return new Response("not found", { status: 404 });
  };

  return {
    cache,
    calls,
    origin,
    get: (path, init) => worker.fetch(new Request(origin + path, init), ENV),
    upstreamCalls: (fragment) => calls.filter((url) => url.includes(fragment)).length,
  };
}

test("the descriptor answers on / and both index spellings, cached once", async () => {
  const ctx = setup();

  for (const path of ["/", "/r", "/r/", "/index.json", "/r/index.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    assert.equal(response.headers.get("access-control-allow-origin"), "*", path);
    assert.deepEqual(await response.json(), DESCRIPTOR, path);
  }
  assert.equal(ctx.upstreamCalls("index.json"), 1, "the descriptor is fetched once, then served from cache");
});

test("repo.json keeps answering on its old path for existing clients", async () => {
  const ctx = setup();

  for (const path of ["/repo.json", "/r/repo.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    const body = await response.json();
    assert.equal(body.schema, 2, path);
    assert.equal(body.packages[0].filename, ASSET, path);
  }
  assert.equal(ctx.upstreamCalls("repo.json"), 1);
});

test("a declared backend index is reachable in both spellings and cached once", async () => {
  const ctx = setup();

  for (const path of ["/apk/index.json", "/r/apk/index.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 200, path);
    const body = await response.json();
    assert.equal(body.mgr, "apk", path);
    assert.equal(body.packages[0].filename, ASSET, path);
  }
  assert.equal(ctx.upstreamCalls("apk/index.json"), 1);
});

test("a manager the descriptor does not declare is a 404 and is never fetched", async () => {
  const ctx = setup();

  const response = await ctx.get("/opkg/index.json");
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { error: "no such index: opkg/index.json" });
  assert.equal(ctx.upstreamCalls("opkg/index.json"), 0, "an undeclared manager must not be proxied");
});

test("paths that leave the r/ tree resolve nowhere", async () => {
  const ctx = setup();

  for (const path of ["/r/../secret.json", "/apk/../../etc/passwd", "/r/%2e%2e/secret.json"]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 404, path);
  }
  assert.equal(ctx.upstreamCalls("secret"), 0);
  assert.equal(ctx.upstreamCalls("passwd"), 0);
});

test("an asset is fetched once and then served from cache under its hash", async () => {
  const ctx = setup();

  const first = await ctx.get(`/latest/${ASSET}`);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-pica-cache"), "miss");
  assert.equal(first.headers.get("cache-control"), "public, max-age=604800, immutable");
  assert.equal(await first.text(), "artifact-bytes");

  const second = await ctx.get(`/latest/${ASSET}`);
  assert.equal(second.status, 200);
  assert.equal(second.headers.get("x-pica-cache"), "hit");
  assert.equal(await second.text(), "artifact-bytes");
  assert.equal(ctx.upstreamCalls(RELEASES), 1);
});

test("republishing a filename under a new hash bypasses the cached copy", async () => {
  const cache = new MemoryCache();
  const before = setup({ cache });
  await before.get(`/latest/${ASSET}`);

  // Let the 60s metadata TTL lapse, the way it would before any real republish.
  cache.entries.delete(new URL("/__cache__/json/repo.json", ORIGIN).toString());

  const after = setup({ cache, repo: repoIndex({ sha256: "b".repeat(64) }) });
  const response = await after.get(`/latest/${ASSET}`);
  assert.equal(
    response.headers.get("x-pica-cache"),
    "miss",
    "a new hash must not be served from the previous artifact's entry",
  );
  assert.equal(after.upstreamCalls(RELEASES), 1);

  const assetKeys = [...cache.entries.keys()].filter((key) => key.includes("/__cache__/asset/"));
  assert.equal(assetKeys.length, 2, "the two hashes coexist rather than overwrite each other");
});

test("a tagged release path uses the tag, not the rolling latest", async () => {
  const ctx = setup();

  const response = await ctx.get(`/releases/26-10-22-09-35-55/${ASSET}`);
  assert.equal(response.status, 200);
  assert.equal(ctx.upstreamCalls(`${RELEASES}26-10-22-09-35-55/${ASSET}`), 1);
});

test("an asset name that is not a package archive is refused before reaching upstream", async () => {
  const ctx = setup();

  for (const path of [
    `/latest/notes.txt`,
    `/latest/../${ASSET}`,
    `/latest/${ASSET}/..%2f..%2fetc%2fpasswd`,
    `/releases/bad%20tag/${ASSET}`,
  ]) {
    const response = await ctx.get(path);
    assert.equal(response.status, 404, path);
  }
  assert.equal(ctx.upstreamCalls(RELEASES), 0);
});

test("a download_url outside this repository is not followed", async () => {
  const ctx = setup({
    repo: repoIndex({ download_url: "https://evil.example/collect" }),
  });

  const response = await ctx.get(`/latest/${ASSET}`);
  assert.equal(response.status, 200);
  assert.equal(
    ctx.upstreamCalls(RELEASES + "latest/" + ASSET),
    1,
    "the worker falls back to the release it controls",
  );
  assert.equal(ctx.upstreamCalls("evil.example"), 0);
});

test("an upstream failure is reported as 502 and not cached", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}repo.json`]: () => new Response("boom", { status: 500 }),
    },
  });

  const response = await ctx.get("/repo.json");
  assert.equal(response.status, 502);
  assert.match((await response.json()).error, /returned 500/);
  assert.equal(ctx.cache.entries.size, 0);

  const retry = await ctx.get("/repo.json");
  assert.equal(retry.status, 502);
  assert.equal(ctx.upstreamCalls("repo.json"), 2, "a failure must not be served from cache");
});

test("a missing index is a 404 rather than a 502", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}pica/index.json`]: () => new Response("gone", { status: 404 }),
    },
  });

  const response = await ctx.get("/pica/index.json");
  assert.equal(response.status, 404);
});

test("a response that is not JSON is refused instead of cached as an index", async () => {
  const ctx = setup({
    overrides: {
      [`${RAW}index.json`]: () => new Response("<html>portal</html>", { status: 200 }),
    },
  });

  const response = await ctx.get("/index.json");
  assert.equal(response.status, 502);
  assert.match((await response.json()).error, /not JSON/);
  assert.equal(ctx.cache.entries.size, 0);
});

test("an artifact the index does not name is still served, but only briefly cached", async () => {
  const ctx = setup({ repo: { schema: 2, packages: [] } });

  const first = await ctx.get(`/latest/${ASSET}`);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("cache-control"), "public, max-age=300");

  await ctx.get(`/latest/${ASSET}`);
  assert.equal(ctx.upstreamCalls(RELEASES), 2, "without a hash there is nothing to key a permanent entry on");
});

test("HEAD returns the headers without the body", async () => {
  const ctx = setup();

  const response = await ctx.get(`/latest/${ASSET}`, { method: "HEAD" });
  assert.equal(response.status, 200);
  assert.equal(response.body, null);
  assert.equal(response.headers.get("etag"), '"abc"');
});

test("preflight is answered, and other methods are refused", async () => {
  const ctx = setup();

  const preflight = await ctx.get(`/latest/${ASSET}`, {
    method: "OPTIONS",
    headers: { origin: ORIGIN },
  });
  assert.equal(preflight.status, 204);
  assert.match(preflight.headers.get("access-control-allow-methods"), /GET/);

  const posted = await ctx.get(`/latest/${ASSET}`, { method: "POST" });
  assert.equal(posted.status, 405);
});

test("an unknown route is a 404 with a JSON body", async () => {
  const ctx = setup();

  const response = await ctx.get("/nothing/here");
  assert.equal(response.status, 404);
  assert.match((await response.json()).error, /no such route/);
});

// A dated release, the shape `build-and-release.yml` publishes alongside the
// rolling `latest`.
const TAG = "26-10-22-09-35-55";
// Real filenames from the live index. `@` separates a package from who built it
// (`ddns-go@immortalwrt`, `rtp2httpd@jarod360`) and `~` appears in an upstream
// version; both are characters `encodeURIComponent` would turn into `%40` and
// `%7E`, which the asset route refuses on sight.
const ASSET_AT = "luci-theme-material3@KawaiiHachimi-26.156.15499~38397ed-1-all-opkg.pkg.tar.gz";

test("a backend index sends clients back to this origin for the artifact", async () => {
  const ctx = setup();

  for (const path of ["/apk/index.json", "/r/apk/index.json"]) {
    const body = await (await ctx.get(path)).json();
    const entry = body.packages[0];
    assert.equal(entry.download_url, `${ORIGIN}/latest/${ASSET}`, path);
    // The address is the only thing that changes: a client verifies these bytes
    // against this hash, so both have to survive untouched.
    assert.equal(entry.sha256, SHA, path);
    assert.equal(entry.filename, ASSET, path);
    assert.equal(entry.pkgver, "1.0", path);
  }

  assert.equal(
    APK_INDEX.packages[0].download_url,
    `${RELEASES}latest/${ASSET}`,
    "the upstream body is not rewritten in place, or the next request would rewrite it twice",
  );
});

test("repo.json is rewritten in both spellings", async () => {
  const ctx = setup();

  for (const path of ["/repo.json", "/r/repo.json"]) {
    const body = await (await ctx.get(path)).json();
    assert.equal(body.packages[0].download_url, `${ORIGIN}/latest/${ASSET}`, path);
    assert.equal(body.schema, 2, path);
  }
});

test("the descriptor is served as published", async () => {
  const ctx = setup();

  const body = await (await ctx.get("/index.json")).json();
  assert.deepEqual(body, DESCRIPTOR, "the descriptor names indexes, not packages");
  assert.equal(body.packages, undefined);
});

test("a rewritten address is not rewritten again", async () => {
  const ctx = setup();

  const first = await (await ctx.get("/apk/index.json")).json();
  // The second request is served from the 60s index cache, so it exercises the
  // other half of the claim: the cache holds the published index, not the
  // rewritten one.
  const second = await (await ctx.get("/apk/index.json")).json();

  assert.equal(first.packages[0].download_url, `${ORIGIN}/latest/${ASSET}`);
  assert.equal(second.packages[0].download_url, first.packages[0].download_url);
  assert.ok(!second.packages[0].download_url.includes("/latest/latest/"));
  assert.equal(ctx.upstreamCalls("apk/index.json"), 1);
});

test("the rewritten address belongs to the origin that was asked", async () => {
  const production = setup({ origin: "https://pica-nest.miaoer.net" });

  const body = await (await production.get("/apk/index.json")).json();
  assert.equal(body.packages[0].download_url, `https://pica-nest.miaoer.net/latest/${ASSET}`);
});

test("an artifact published under a dated release keeps its tag", async () => {
  const ctx = setup({ repo: repoIndex({ download_url: `${RELEASES}${TAG}/${ASSET}` }) });

  const body = await (await ctx.get("/repo.json")).json();
  assert.equal(body.packages[0].download_url, `${ORIGIN}/releases/${TAG}/${ASSET}`);

  // And the address it was given actually serves the artifact.
  const response = await ctx.get(`/releases/${TAG}/${ASSET}`);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "artifact-bytes");
});

test("the proxy still resolves its upstream from the index as published", async () => {
  const ctx = setup({ repo: repoIndex({ download_url: `${RELEASES}${TAG}/${ASSET}` }) });

  await ctx.get("/repo.json");
  const response = await ctx.get(`/releases/${TAG}/${ASSET}`);
  assert.equal(response.status, 200);

  // `serveAsset` reads the cached index to choose its upstream. Were the cached
  // copy the rewritten one, the tagged address would no longer be a github.com
  // URL and the artifact would silently be fetched from the rolling release
  // instead — different bytes, under the hash recorded for these.
  assert.equal(ctx.upstreamCalls(`${RELEASES}${TAG}/${ASSET}`), 1);
  assert.equal(ctx.upstreamCalls(`${RELEASES}latest/`), 0);
});

test("a filename the proxy route would refuse is left as published", async () => {
  const ctx = setup({
    repo: repoIndex({ filename: "notes.txt", download_url: `${RELEASES}latest/notes.txt` }),
  });

  const body = await (await ctx.get("/repo.json")).json();
  assert.equal(
    body.packages[0].download_url,
    `${RELEASES}latest/notes.txt`,
    "rewriting it would turn a working address into a 404",
  );
});

test("a filename carrying @ or ~ is rewritten without percent-encoding", async () => {
  const ctx = setup({
    repo: repoIndex({ filename: ASSET_AT, download_url: `${RELEASES}latest/${ASSET_AT}` }),
  });

  const body = await (await ctx.get("/repo.json")).json();
  const url = body.packages[0].download_url;
  assert.equal(url, `${ORIGIN}/latest/${ASSET_AT}`);
  assert.ok(!url.includes("%"), `an encoded name would be refused by the asset route: ${url}`);

  const response = await ctx.get(`/latest/${ASSET_AT}`);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "artifact-bytes");
});

test("an address this worker does not own is left as published", async () => {
  const cases = {
    "another host": "https://evil.example/collect",
    "another repository's release": `https://github.com/someone/else/releases/latest/${ASSET}`,
    "a LAN mirror": `http://192.168.1.10/repo/${ASSET}`,
  };

  for (const [name, download_url] of Object.entries(cases)) {
    const ctx = setup({ repo: repoIndex({ download_url }) });
    const body = await (await ctx.get("/repo.json")).json();
    assert.equal(body.packages[0].download_url, download_url, name);
  }
});

test("an entry with no address is not given one", async () => {
  const ctx = setup({ repo: repoIndex({ download_url: undefined }) });

  const body = await (await ctx.get("/repo.json")).json();
  assert.equal(
    "download_url" in body.packages[0],
    false,
    "inventing an address would hide an index that published none",
  );
});

test("an entry whose filename disagrees with its address is left as published", async () => {
  const ctx = setup({
    repo: repoIndex({ download_url: `${RELEASES}latest/something-else.pkg.tar.gz` }),
  });

  const body = await (await ctx.get("/repo.json")).json();
  assert.equal(
    body.packages[0].download_url,
    `${RELEASES}latest/something-else.pkg.tar.gz`,
    "the sha256 beside the entry describes the published file, not the rewritten one",
  );
});
