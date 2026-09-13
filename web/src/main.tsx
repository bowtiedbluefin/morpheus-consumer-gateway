import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Model = {
  id: string;
  alias: string;
  enabled: boolean;
  duration_seconds: number;
  retention: "on_demand" | "until_expiry" | "maintain";
  idle_seconds: number;
  max_sessions: number;
  warm_until: number | null;
};
type Policy = {
  revision: number;
  models: Model[];
  providers: { mode: "all" | "allowlist"; allow: string[]; deny: string[] };
  weights: Record<string, number>;
  max_sessions: number;
  queue_seconds: number;
  paused: boolean;
};
type Session = {
  id: string;
  chain_id: string | null;
  alias: string;
  provider: string;
  state: string;
  ends_at: number;
  stake_wei: string;
  busy: boolean;
};
type Key = {
  id: string;
  name: string;
  prefix: string;
  enabled: boolean;
  created_at: number;
  last_used: number | null;
  models: string[];
  concurrency: number;
  requests_per_minute: number;
};
type Operation = {
  id: string;
  kind: string;
  state: string;
  message: string;
  created_at: number;
};
type Status = {
  demo: boolean;
  identity: { wallet: string; chain: string; version: string } | null;
  balances: { mor: string; eth: string } | null;
  held: unknown;
  node_error: string | null;
  last_error: string | null;
  active_requests: number;
  queued: number;
  maintenance: boolean;
  paused: boolean;
  helper_connected: boolean;
  base_url: string;
  rating_pending: boolean;
};
type Bid = {
  id: string;
  provider: string;
  score: number;
  price_per_second_wei: string;
  eligible: boolean;
  reason: string;
};
type CatalogModel = {
  Id: string;
  Name: string;
  ModelType: string;
  IsDeleted: boolean;
};

let csrf = "";
async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const res = await fetch("/admin/api" + path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      data.error?.message || data.detail || `Request failed (${res.status})`,
    );
  return data;
}
const short = (s: string | null) =>
  s ? s.slice(0, 8) + "…" + s.slice(-6) : "Awaiting confirmation";
const when = (n: number | null) =>
  n ? new Date(n * 1000).toLocaleString() : "—";
function mor(value: string | undefined) {
  try {
    const n = BigInt(value || "0");
    return `${n / 10n ** 18n}.${(n % 10n ** 18n).toString().padStart(18, "0").slice(0, 4)}`;
  } catch {
    return "—";
  }
}
const labels: Record<string, string> = {
  tps: "Generation speed",
  ttft: "Time to first token",
  duration: "Session history",
  success: "Success rate",
  stake: "Provider stake",
};
const pages = [
  "Overview",
  "Models",
  "Providers & rating",
  "Sessions",
  "API keys",
  "Node & activity",
];

function App() {
  const [signed, setSigned] = useState(false),
    [checking, setChecking] = useState(true),
    [secret, setSecret] = useState("");
  const [page, setPage] = useState("Overview"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [policy, setPolicy] = useState<Policy | null>(null),
    [dirty, setDirty] = useState(false),
    [status, setStatus] = useState<Status | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]),
    [keys, setKeys] = useState<Key[]>([]),
    [operations, setOperations] = useState<Operation[]>([]);
  const [catalog, setCatalog] = useState<CatalogModel[]>([]),
    [bids, setBids] = useState<Bid[]>([]),
    [selected, setSelected] = useState("");
  const [keyName, setKeyName] = useState(""),
    [keyModel, setKeyModel] = useState(""),
    [newKey, setNewKey] = useState("");
  const [immediate, setImmediate] = useState(false),
    [recoveries, setRecoveries] = useState<Record<string, string>>({});
  const [walletSessions, setWalletSessions] = useState<unknown>(null);

  async function refresh() {
    const results = await Promise.allSettled([
      api<Status>("/status"),
      api<{ sessions: Session[] }>("/sessions"),
      api<{ keys: Key[] }>("/keys"),
      api<{ operations: Operation[] }>("/operations"),
    ]);
    const [s, se, k, o] = results;
    if (s.status === "fulfilled") setStatus(s.value);
    if (se.status === "fulfilled") setSessions(se.value.sessions);
    if (k.status === "fulfilled") setKeys(k.value.keys);
    if (o.status === "fulfilled") setOperations(o.value.operations);
    const failed = results.find((x) => x.status === "rejected");
    if (failed?.status === "rejected") setError(String(failed.reason.message));
  }
  async function load() {
    setPolicy(await api<Policy>("/policy"));
    await refresh();
  }
  useEffect(() => {
    api<{ csrf: string }>("/me")
      .then(async (data) => {
        csrf = data.csrf;
        setSigned(true);
        await load();
      })
      .catch(() => {})
      .finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    if (!signed) return;
    const timer = setInterval(() => {
      void refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [signed]);
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  function change(next: Policy) {
    setPolicy(next);
    setDirty(true);
  }
  function updateModel(index: number, patch: Partial<Model>) {
    if (policy)
      change({
        ...policy,
        models: policy.models.map((m, i) =>
          i === index ? { ...m, ...patch } : m,
        ),
      });
  }
  async function save() {
    if (policy) {
      setPolicy(await api<Policy>("/policy", "PUT", policy));
      setDirty(false);
      setNotice(
        "Settings saved. Provider restrictions apply now; rating changes require Apply and restart.",
      );
    }
  }
  async function restart(apply: boolean) {
    await api("/node/restart", "POST", { immediate, apply_rating: apply });
    setNotice("Restart scheduled. Follow its progress below.");
  }
  function addModel(model?: CatalogModel) {
    if (!policy) return;
    change({
      ...policy,
      models: [
        ...policy.models,
        {
          id: model?.Id || "",
          alias:
            model?.Name?.toLowerCase().replace(/[^a-z0-9._-]+/g, "-") || "",
          enabled: true,
          duration_seconds: 1800,
          retention: "on_demand",
          idle_seconds: 300,
          max_sessions: 1,
          warm_until: null,
        },
      ],
    });
  }

  if (checking)
    return <div className="loading">Connecting to your gateway…</div>;
  if (!signed)
    return (
      <main className="login">
        <div className="login-brand">M / MORPHEUS</div>
        <section className="login-card">
          <span className="eyebrow">YOUR NODE. YOUR ACCESS.</span>
          <h1>
            Your gateway
            <br />
            to Morpheus.
          </h1>
          <p>
            Manage your consumer node, choose your providers, and give your
            applications one API.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () => {
                const data = await api<{ csrf: string }>("/login", "POST", {
                  token: secret,
                });
                csrf = data.csrf;
                setSecret("");
                setSigned(true);
                await load();
              });
            }}
          >
            <label>
              Administrator secret
              <input
                type="password"
                autoComplete="current-password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                required
                placeholder="From your installation's secrets/admin-token"
              />
            </label>
            <button disabled={busy} type="submit">
              {busy ? "Signing in…" : "Open dashboard →"}
            </button>
          </form>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <small>
            This secret administers your installation. Applications use separate
            API keys.
          </small>
        </section>
        <footer>SELF-HOSTED CONSUMER GATEWAY · 0.1.0</footer>
      </main>
    );
  if (!policy)
    return (
      <div className="loading">
        {error || "Loading your settings…"}
        <button onClick={() => void action(load)}>Retry</button>
      </div>
    );
  const open = sessions.filter((s) => s.state !== "closed");
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span className="mark">M</span>
          <div>
            MORPHEUS<small>CONSUMER GATEWAY</small>
          </div>
        </div>
        <nav aria-label="Main navigation">
          {pages.map((p, i) => (
            <button
              key={p}
              className={page === p ? "nav active" : "nav"}
              onClick={() => setPage(p)}
            >
              <span className="nav-number">0{i + 1}</span>
              {p}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={"dot " + (status?.identity ? "green" : "")} />
          {status?.identity ? "Consumer node connected" : "Waiting for node"}
          <small>Local control · v0.1.0</small>
          <button
            className="link"
            onClick={() =>
              void action(async () => {
                await api("/logout", "POST");
                setSigned(false);
                setNewKey("");
              })
            }
          >
            Sign out
          </button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">YOUR INFRASTRUCTURE / YOUR RULES</span>
            <h1>{page}</h1>
          </div>
          <span className="pill">
            {status?.maintenance
              ? "Restart in progress"
              : status?.paused
                ? "Paused"
                : status?.node_error
                  ? "Node unavailable"
                  : "Gateway online"}
          </span>
        </header>
        {status?.demo && (
          <div className="banner warning">
            DEMO · Simulated node and providers. No wallet, transactions or real
            inference.
          </div>
        )}
        {error && (
          <div className="banner error" role="alert">
            {error}
            <button className="link" onClick={() => setError("")}>
              Dismiss
            </button>
          </div>
        )}
        {notice && (
          <div className="banner success" role="status">
            {notice}
          </div>
        )}
        {dirty && (
          <div className="banner unsaved">
            You have unsaved settings.
            <button disabled={busy} onClick={() => void action(save)}>
              Save settings
            </button>
          </div>
        )}
        {status?.node_error && (
          <div className="banner warning">{status.node_error}</div>
        )}
        {page === "Overview" && (
          <>
            <section className="hero">
              <div>
                <span className="eyebrow">
                  ONE ENDPOINT. YOUR CONSUMER NODE.
                </span>
                <h2>Inference, on your terms.</h2>
                <p>
                  Your applications send a model and a prompt.
                  <br />
                  Your gateway takes care of the session.
                </p>
                <div className="endpoint">
                  <code>{status?.base_url || "Connecting…"}</code>
                  <button
                    className="secondary"
                    onClick={() =>
                      void action(async () => {
                        await navigator.clipboard.writeText(
                          status?.base_url || "",
                        );
                        setNotice("Base URL copied.");
                      })
                    }
                  >
                    Copy URL
                  </button>
                </div>
              </div>
              <div className="hero-symbol" aria-hidden="true">
                M<span>CONNECTED COMPUTE</span>
              </div>
            </section>
            <div className="stats">
              <Stat
                label="Open & pending sessions"
                value={String(open.length)}
                foot={`${status?.active_requests || 0} active requests`}
              />
              <Stat
                label="Liquid MOR"
                value={mor(status?.balances?.mor)}
                foot="Available in your node wallet"
              />
              <Stat
                label="ETH for gas"
                value={mor(status?.balances?.eth)}
                foot="Used for node transactions"
              />
              <Stat
                label="Enabled models"
                value={String(policy.models.filter((m) => m.enabled).length)}
                foot={`${status?.queued || 0} requests waiting`}
              />
            </div>
            <div className="two-col">
              <section className="card">
                <h3>Get connected</h3>
                <ol className="steps">
                  <li>
                    <strong>Choose your models</strong>
                    <p>
                      Set a model alias, session duration and retention policy.
                    </p>
                  </li>
                  <li>
                    <strong>Set your provider rules</strong>
                    <p>
                      Choose who can serve requests and how they are ranked.
                    </p>
                  </li>
                  <li>
                    <strong>Create an application key</strong>
                    <p>
                      Use your base URL and key in an OpenAI-compatible client.
                    </p>
                  </li>
                </ol>
                <button onClick={() => setPage("Models")}>
                  Configure models →
                </button>
              </section>
              <section className="card">
                <h3>Your node</h3>
                <dl>
                  <dt>Wallet</dt>
                  <dd className="mono">
                    {short(status?.identity?.wallet || null)}
                  </dd>
                  <dt>Network chain ID</dt>
                  <dd>{status?.identity?.chain || "—"}</dd>
                  <dt>Node version</dt>
                  <dd>{status?.identity?.version || "—"}</dd>
                  <dt>Rating configuration</dt>
                  <dd>
                    {status?.rating_pending ? "Pending apply" : "Applied"}
                  </dd>
                </dl>
                <p className="muted">
                  Opening a session escrows MOR. Closed sessions may leave some
                  MOR time-locked until it becomes claimable.
                </p>
                {Boolean(status?.held) && (
                  <details>
                    <summary>
                      Held and claimable balances (node response, wei)
                    </summary>
                    <pre>{JSON.stringify(status?.held, null, 2)}</pre>
                  </details>
                )}
              </section>
            </div>
          </>
        )}
        {page === "Models" && (
          <>
            <div className="section-intro">
              <p>
                Choose the models your applications can use and how long their
                sessions stay open.
              </p>
              <div className="actions">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void action(async () => {
                      setCatalog(
                        (await api<{ models: CatalogModel[] }>("/catalog"))
                          .models,
                      );
                    })
                  }
                >
                  Browse node catalog
                </button>
                <button onClick={() => addModel()}>+ Add model by ID</button>
              </div>
            </div>
            {catalog.length > 0 && (
              <section className="card">
                <h3>Models registered on your network</h3>
                <div className="catalog">
                  {catalog
                    .filter(
                      (m) =>
                        !policy.models.some(
                          (p) => p.id.toLowerCase() === m.Id.toLowerCase(),
                        ),
                    )
                    .map((m) => (
                      <button
                        className="secondary"
                        key={m.Id}
                        onClick={() => addModel(m)}
                      >
                        {m.Name || short(m.Id)} <small>{m.ModelType}</small> +
                      </button>
                    ))}
                </div>
              </section>
            )}
            {policy.models.length === 0 && (
              <Empty
                title="Start with your first model"
                text="Browse the node's catalog or paste an on-chain model ID. Give it a readable alias for your applications."
              />
            )}
            {policy.models.map((m, i) => (
              <section className="card model-card" key={i}>
                <div className="card-head">
                  <h3>{m.alias || `Model ${i + 1}`}</h3>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={m.enabled}
                      onChange={(e) =>
                        updateModel(i, { enabled: e.target.checked })
                      }
                    />
                    Enabled
                  </label>
                </div>
                <div className="form-grid">
                  <label>
                    API model alias
                    <input
                      value={m.alias}
                      onChange={(e) =>
                        updateModel(i, { alias: e.target.value })
                      }
                      placeholder="my-model"
                    />
                  </label>
                  <label>
                    On-chain model ID
                    <input
                      className="mono"
                      value={m.id}
                      onChange={(e) => updateModel(i, { id: e.target.value })}
                      placeholder="0x… (64 hex characters)"
                    />
                  </label>
                  <label>
                    Session duration (minutes)
                    <input
                      type="number"
                      min="5"
                      max="1440"
                      value={m.duration_seconds / 60}
                      onChange={(e) =>
                        updateModel(i, {
                          duration_seconds: Math.round(
                            Number(e.target.value) * 60,
                          ),
                        })
                      }
                    />
                  </label>
                  <label>
                    Keep sessions
                    <select
                      value={m.retention}
                      onChange={(e) =>
                        updateModel(i, {
                          retention: e.target.value as Model["retention"],
                        })
                      }
                    >
                      <option value="on_demand">
                        On demand — close when idle
                      </option>
                      <option value="until_expiry">
                        Until expiry — do not idle-close
                      </option>
                      <option value="maintain">
                        Maintain availability — renew automatically
                      </option>
                    </select>
                  </label>
                  {m.retention === "on_demand" && (
                    <label>
                      Close after idle (minutes)
                      <input
                        type="number"
                        min="0"
                        value={m.idle_seconds / 60}
                        onChange={(e) =>
                          updateModel(i, {
                            idle_seconds: Math.round(
                              Number(e.target.value) * 60,
                            ),
                          })
                        }
                      />
                    </label>
                  )}
                  <label>
                    Maximum sessions for this model
                    <input
                      type="number"
                      min="1"
                      max="8"
                      value={m.max_sessions}
                      onChange={(e) =>
                        updateModel(i, { max_sessions: Number(e.target.value) })
                      }
                    />
                  </label>
                  {m.retention === "maintain" && (
                    <label>
                      Keep ready until (local time; blank = continuously)
                      <input
                        type="datetime-local"
                        value={
                          m.warm_until
                            ? new Date(
                                m.warm_until * 1000 -
                                  new Date().getTimezoneOffset() * 60000,
                              )
                                .toISOString()
                                .slice(0, 16)
                            : ""
                        }
                        onChange={(e) =>
                          updateModel(i, {
                            warm_until: e.target.value
                              ? Math.floor(
                                  new Date(e.target.value).getTime() / 1000,
                                )
                              : null,
                          })
                        }
                      />
                    </label>
                  )}
                </div>
                {m.retention === "maintain" && (
                  <p className="warning-inline">
                    This opens replacement sessions automatically and may lock
                    additional MOR. A hot session does not reserve a dedicated
                    GPU.
                  </p>
                )}
                <div className="actions">
                  <button
                    className="secondary"
                    disabled={busy || dirty || !m.enabled}
                    onClick={() =>
                      void action(async () => {
                        await api(`/models/${m.id}/prewarm`, "POST");
                        setNotice("Session is ready.");
                      })
                    }
                  >
                    Open session now
                  </button>
                  <button
                    className="danger link"
                    onClick={() =>
                      change({
                        ...policy,
                        models: policy.models.filter((_, j) => j !== i),
                      })
                    }
                  >
                    Remove model
                  </button>
                </div>
              </section>
            ))}
            <section className="card">
              <h3>Installation limits</h3>
              <div className="form-grid">
                <label>
                  Maximum total sessions
                  <input
                    type="number"
                    min="1"
                    max="32"
                    value={policy.max_sessions}
                    onChange={(e) =>
                      change({
                        ...policy,
                        max_sessions: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  Queue wait (seconds)
                  <input
                    type="number"
                    min="1"
                    max="300"
                    value={policy.queue_seconds}
                    onChange={(e) =>
                      change({
                        ...policy,
                        queue_seconds: Number(e.target.value),
                      })
                    }
                  />
                </label>
              </div>
              <p className="muted">
                Pending opens and unconfirmed closes count against the limit.
                Session count limits do not guarantee a maximum MOR amount.
              </p>
            </section>
          </>
        )}
        {page === "Providers & rating" && (
          <>
            <div className="section-intro">
              <p>
                Provider rules apply to every session selected by this gateway.
                Denied providers always take precedence.
              </p>
            </div>
            <section className="card">
              <h3>Provider access</h3>
              <label>
                Allowed providers
                <select
                  value={policy.providers.mode}
                  onChange={(e) =>
                    change({
                      ...policy,
                      providers: {
                        ...policy.providers,
                        mode: e.target.value as "all" | "allowlist",
                      },
                    })
                  }
                >
                  <option value="all">
                    All providers except those blocked below
                  </option>
                  <option value="allowlist">
                    Only the providers listed below
                  </option>
                </select>
              </label>
              <div className="form-grid">
                <label>
                  Allowlist (one address per line)
                  <textarea
                    spellCheck={false}
                    value={policy.providers.allow.join("\n")}
                    onChange={(e) =>
                      change({
                        ...policy,
                        providers: {
                          ...policy.providers,
                          allow: e.target.value.split("\n"),
                        },
                      })
                    }
                    onBlur={() =>
                      change({
                        ...policy,
                        providers: {
                          ...policy.providers,
                          allow: policy.providers.allow
                            .map((x) => x.trim())
                            .filter(Boolean),
                        },
                      })
                    }
                    placeholder="0x…"
                  />
                </label>
                <label>
                  Blocklist (one address per line)
                  <textarea
                    spellCheck={false}
                    value={policy.providers.deny.join("\n")}
                    onChange={(e) =>
                      change({
                        ...policy,
                        providers: {
                          ...policy.providers,
                          deny: e.target.value.split("\n"),
                        },
                      })
                    }
                    onBlur={() =>
                      change({
                        ...policy,
                        providers: {
                          ...policy.providers,
                          deny: policy.providers.deny
                            .map((x) => x.trim())
                            .filter(Boolean),
                        },
                      })
                    }
                    placeholder="0x…"
                  />
                </label>
              </div>
              {policy.providers.mode === "allowlist" &&
                !policy.providers.allow.filter(Boolean).length && (
                  <p className="warning-inline">
                    Your allowlist is empty. No provider will be eligible.
                  </p>
                )}
            </section>
            <section className="card">
              <div className="card-head">
                <h3>Provider rating</h3>
                <span className="pill">
                  {status?.rating_pending ? "Pending apply" : "Applied"}
                </span>
              </div>
              <p className="muted">
                Five weights, totaling 100%. The node also adjusts the final
                score for provider price.
              </p>
              <div className="weights">
                {Object.entries(policy.weights).map(([k, v]) => (
                  <label key={k}>
                    {labels[k]}
                    <div className="number-unit">
                      <input
                        aria-label={labels[k]}
                        type="number"
                        min="0"
                        max="100"
                        step="1"
                        value={Math.round(v * 10000) / 100}
                        onChange={(e) =>
                          change({
                            ...policy,
                            weights: {
                              ...policy.weights,
                              [k]: Number(e.target.value) / 100,
                            },
                          })
                        }
                      />
                      <span>%</span>
                    </div>
                  </label>
                ))}
              </div>
              <p
                className={
                  Math.abs(
                    Object.values(policy.weights).reduce((a, b) => a + b, 0) -
                      1,
                  ) > 1e-9
                    ? "error"
                    : "muted"
                }
              >
                Total:{" "}
                {Math.round(
                  Object.values(policy.weights).reduce((a, b) => a + b, 0) *
                    10000,
                ) / 100}
                %
              </p>
              <div className="actions">
                <button disabled={busy} onClick={() => void action(save)}>
                  Save settings
                </button>
                <button
                  className="secondary"
                  disabled={
                    busy ||
                    dirty ||
                    !status?.helper_connected ||
                    status.maintenance
                  }
                  onClick={() => void action(() => restart(true))}
                >
                  Apply and restart node
                </button>
              </div>
              <p className="muted">
                Save first. Apply restarts your node after active requests
                finish. The dashboard stays available.
              </p>
            </section>
            <section className="card">
              <h3>Inspect current ranking</h3>
              <div className="actions">
                <select
                  aria-label="Model to inspect"
                  value={selected}
                  onChange={(e) => setSelected(e.target.value)}
                >
                  <option value="">Choose a saved model</option>
                  {policy.models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.alias}
                    </option>
                  ))}
                </select>
                <button
                  className="secondary"
                  disabled={!selected || dirty || busy}
                  onClick={() =>
                    void action(async () => {
                      setBids(
                        (await api<{ bids: Bid[] }>(`/models/${selected}/bids`))
                          .bids,
                      );
                    })
                  }
                >
                  Load rated bids
                </button>
              </div>
              <p className="muted">
                This shows the node's currently applied ranking, not a preview
                of unsaved weights.
              </p>
              {bids.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Provider</th>
                        <th>Score</th>
                        <th>MOR / second</th>
                        <th>Gateway policy</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bids.map((b) => (
                        <tr key={b.id}>
                          <td className="mono" title={b.provider}>
                            {short(b.provider)}
                          </td>
                          <td>{b.score.toFixed(4)}</td>
                          <td>{mor(b.price_per_second_wei)}</td>
                          <td>{b.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}
        {page === "Sessions" && (
          <>
            <div className="section-intro">
              <p>
                Sessions belong to your consumer wallet. An expired session may
                still need an on-chain close before funds return.
              </p>
            </div>
            {sessions.length === 0 ? (
              <Empty
                title="No sessions yet"
                text="Send your first API request or open a session from Models. Your gateway will record it here."
              />
            ) : (
              <section className="card">
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Model / session</th>
                        <th>Provider</th>
                        <th>Status</th>
                        <th>Expires</th>
                        <th>Escrow MOR</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sessions.map((s) => (
                        <React.Fragment key={s.id}>
                          <tr>
                            <td>
                              <strong>{s.alias}</strong>
                              <small
                                className="mono"
                                title={s.chain_id || s.id}
                              >
                                {short(s.chain_id)}
                              </small>
                            </td>
                            <td className="mono" title={s.provider}>
                              {short(s.provider)}
                            </td>
                            <td>
                              <span className="pill">
                                {s.busy
                                  ? "Serving request"
                                  : s.state === "open" &&
                                      s.ends_at < Date.now() / 1000
                                    ? "Expired · unclosed"
                                    : s.state.replaceAll("_", " ")}
                              </span>
                            </td>
                            <td>{when(s.ends_at)}</td>
                            <td>{mor(s.stake_wei)}</td>
                            <td>
                              <button
                                className="secondary"
                                disabled={
                                  busy || s.state === "closed" || !s.chain_id
                                }
                                onClick={() =>
                                  void action(async () => {
                                    await api(
                                      `/sessions/${s.id}/close`,
                                      "POST",
                                    );
                                    setNotice(
                                      "Close requested; active work will drain first.",
                                    );
                                  })
                                }
                              >
                                Close
                              </button>
                            </td>
                          </tr>
                          {s.state === "open_unknown" && (
                            <tr>
                              <td colSpan={6}>
                                <p className="warning-inline">
                                  Opening result is uncertain. New sessions are
                                  paused to prevent duplicates. Inspect wallet
                                  sessions and enter the matching session ID.
                                </p>
                                <div className="actions">
                                  <input
                                    aria-label="Recovered session ID"
                                    placeholder="0x… session ID"
                                    value={recoveries[s.id] || ""}
                                    onChange={(e) =>
                                      setRecoveries({
                                        ...recoveries,
                                        [s.id]: e.target.value,
                                      })
                                    }
                                  />
                                  <button
                                    disabled={busy}
                                    onClick={() =>
                                      void action(async () => {
                                        await api(
                                          `/sessions/${s.id}/bind`,
                                          "POST",
                                          { session_id: recoveries[s.id] },
                                        );
                                        setNotice(
                                          "Session verified and recovered.",
                                        );
                                      })
                                    }
                                  >
                                    Verify and recover
                                  </button>
                                </div>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
            <section className="card">
              <h3>Wallet session recovery</h3>
              <p className="muted">
                Read the latest 100 on-chain wallet sessions. Sessions created
                outside this gateway are not automatically adopted or closed.
              </p>
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  void action(async () =>
                    setWalletSessions(await api("/wallet/sessions")),
                  )
                }
              >
                Inspect wallet sessions
              </button>
              {walletSessions !== null && (
                <pre>{JSON.stringify(walletSessions, null, 2)}</pre>
              )}
            </section>
          </>
        )}
        {page === "API keys" && (
          <>
            <div className="section-intro">
              <p>
                Issue a separate key for each application. Keys can send
                inference requests; they cannot administer your node.
              </p>
            </div>
            <section className="card">
              <h3>Create an application key</h3>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action(async () => {
                    const data = await api<{ key: string }>("/keys", "POST", {
                      name: keyName,
                      models: keyModel ? [keyModel] : [],
                      concurrency: 2,
                      requests_per_minute: 60,
                    });
                    setNewKey(data.key);
                    setKeyName("");
                  });
                }}
              >
                <div className="form-grid">
                  <label>
                    Application name
                    <input
                      required
                      value={keyName}
                      onChange={(e) => setKeyName(e.target.value)}
                      placeholder="My coding assistant"
                    />
                  </label>
                  <label>
                    Model access
                    <select
                      value={keyModel}
                      onChange={(e) => setKeyModel(e.target.value)}
                    >
                      <option value="">All enabled models</option>
                      {policy.models.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.alias}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <button type="submit" disabled={busy || !keyName}>
                  Create key
                </button>
              </form>
              {newKey && (
                <div className="secret-box">
                  <strong>
                    Copy this key now. It will not be shown again.
                  </strong>
                  <code>{newKey}</code>
                  <div className="actions">
                    <button
                      className="secondary"
                      onClick={() =>
                        void action(async () => {
                          await navigator.clipboard.writeText(newKey);
                          setNotice("API key copied.");
                        })
                      }
                    >
                      Copy key
                    </button>
                    <button className="link" onClick={() => setNewKey("")}>
                      I've saved it
                    </button>
                  </div>
                </div>
              )}
            </section>
            <section className="card">
              <h3>Your application keys</h3>
              {keys.length === 0 ? (
                <p className="muted">No keys created yet.</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Prefix</th>
                        <th>Last used</th>
                        <th>Limits</th>
                        <th>Status</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {keys.map((k) => (
                        <tr key={k.id}>
                          <td>{k.name}</td>
                          <td className="mono">{k.prefix}…</td>
                          <td>{when(k.last_used)}</td>
                          <td>
                            {k.requests_per_minute}/min · {k.concurrency}{" "}
                            concurrent
                          </td>
                          <td>{k.enabled ? "Active" : "Revoked"}</td>
                          <td>
                            <button
                              className="link danger"
                              disabled={!k.enabled || busy}
                              onClick={() =>
                                void action(async () => {
                                  await api(`/keys/${k.id}`, "DELETE");
                                  setNotice("Key revoked.");
                                })
                              }
                            >
                              Revoke
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
            <section className="card">
              <h3>Use your gateway</h3>
              <pre>{`curl ${status?.base_url || "https://your-domain/v1"}/chat/completions \\\n  -H "Authorization: Bearer YOUR_APPLICATION_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify({ model: policy.models[0]?.alias || "your-model", messages: [{ role: "user", content: "Hello!" }], stream: true })}'`}</pre>
            </section>
          </>
        )}
        {page === "Node & activity" && (
          <>
            <section className="card">
              <h3>Local node control</h3>
              <p>
                These actions run on your VM. Your dashboard remains available
                while the consumer node restarts.
              </p>
              <label className="check">
                <input
                  type="checkbox"
                  checked={immediate}
                  onChange={(e) => setImmediate(e.target.checked)}
                />
                Restart immediately, interrupting active requests
              </label>
              <div className="actions">
                <button
                  disabled={
                    busy || !status?.helper_connected || status.maintenance
                  }
                  onClick={() => void action(() => restart(false))}
                >
                  Restart node
                </button>
                <button
                  className="secondary"
                  disabled={
                    busy ||
                    dirty ||
                    !status?.helper_connected ||
                    status.maintenance
                  }
                  onClick={() => void action(() => restart(true))}
                >
                  Apply rating and restart
                </button>
              </div>
              {!status?.helper_connected && (
                <p className="warning-inline">
                  Attach mode: local management helper is not configured.
                  Install the managed deployment to enable node restarts.
                </p>
              )}
              <label className="check">
                <input
                  type="checkbox"
                  checked={policy.paused}
                  onChange={(e) =>
                    change({ ...policy, paused: e.target.checked })
                  }
                />
                Pause new inference admissions and automatic prewarming
              </label>
              <p className="muted">
                Save to apply pause changes. Existing requests finish; on-demand
                idle sessions can still close.
              </p>
            </section>
            <section className="card">
              <h3>Restart and configuration operations</h3>
              {!operations.length ? (
                <p className="muted">No operations yet.</p>
              ) : (
                operations.map((o) => (
                  <div className="operation" key={o.id}>
                    <span className="pill">{o.state}</span>
                    <div>
                      <strong>{o.kind.replaceAll("_", " ")}</strong>
                      <p>{o.message}</p>
                      <small>{when(o.created_at)}</small>
                    </div>
                  </div>
                ))
              )}
            </section>
            {status?.last_error && (
              <div className="banner warning">{status.last_error}</div>
            )}
          </>
        )}
        <footer className="main-footer">
          Morpheus Consumer Gateway{" "}
          <span>Your wallet · Your providers · Your endpoint</span>
        </footer>
      </main>
    </div>
  );
}
function Stat({
  label,
  value,
  foot,
}: {
  label: string;
  value: string;
  foot: string;
}) {
  return (
    <section className="stat">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{foot}</small>
    </section>
  );
}
function Empty({ title, text }: { title: string; text: string }) {
  return (
    <section className="empty">
      <span className="empty-mark">M</span>
      <h3>{title}</h3>
      <p>{text}</p>
    </section>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
