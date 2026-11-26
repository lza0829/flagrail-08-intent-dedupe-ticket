import React, { useEffect, useMemo, useState } from "react";
import { get, post, postFixture } from "../api.js";

const 示例V = "checkout.new_tax@3";
const 示例B = "checkout.new_tax-gray10";
const 新票号 = () => `票-ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;

function 状态徽章(s) {
  const cls = s === "已下发" ? "通" : s === "已作废" ? "警" : s === "已回滚" || s === "已失效" ? "败" : "信";
  return <span className={`徽章 ${cls}`}>{s}</span>;
}

function 核证面板({ cert }) {
  if (!cert) return null;
  const good = !!cert.valid;
  return (
    <div className={`核证卡 ${good ? "" : "坏"}`}>
      <div className="行" style={{ marginBottom: 2 }}>
        <strong>意图核证</strong>
        <span className={`徽章 ${good ? "通" : "败"}`}>{good ? "valid" : "invalid"}</span>
        <span className="徽章 信">口径 · {cert.口径 || "世代顶替"}</span>
        {cert.存在 === false && <span className="徽章 信">该意图暂无票</span>}
      </div>
      <div className="意图键码">意图键：{cert.意图键 || "—（库中暂无意图，下发票后自动取最新）"}</div>
      <div className="核证网格">
        <span>当前意图世代：<b>{cert.当前意图世代 ?? "—"}</b></span>
        <span>存活：<b>{(cert.存活票号 || []).length}</b> 张</span>
        <span>作废：<b>{(cert.作废票号 || []).length}</b> 张</span>
        <span>回读归属唯一：<b>{cert.回读归属唯一 ? "是" : "否"}</b></span>
        <span>归属票号：<b>{cert.回读归属票号 || "—"}</b></span>
      </div>
      <div>
        <span className="链路">存活票：</span>
        {(cert.存活票号 || []).length
          ? cert.存活票号.map((t) => <span key={t} className="mini票 活">{t}</span>)
          : <span className="mini票 无">无</span>}
      </div>
      <div style={{ marginTop: 4 }}>
        <span className="链路">作废票：</span>
        {(cert.作废票号 || []).length
          ? cert.作废票号.map((t) => <span key={t} className="mini票 废">{t}</span>)
          : <span className="mini票 无">无</span>}
      </div>
      {!!(cert.问题 || []).length && (
        <p className="讯" style={{ color: "var(--败)" }}>问题：{cert.问题.join("；")}</p>
      )}
    </div>
  );
}

export default function 下发票() {
  const [rows, setRows] = useState([]);
  const [cert, setCert] = useState(null);
  const [讯, set讯] = useState("");
  const [form, setForm] = useState({ 票号: "票-checkout.new_tax@3-gray10", 版本键: 示例V, 桶键: 示例B, 业务世代: "" });
  const [并行, set并行] = useState("");
  const [自动, set自动] = useState(null);

  const focusKey = useMemo(() => rows[0]?.意图键 || "", [rows]);

  const 刷 = async (key) => {
    const list = await get("/下发票");
    setRows(list);
    const k = key || list[0]?.意图键;
    const c = await get(k ? `/核证?意图键=${encodeURIComponent(k)}` : "/核证");
    setCert(c);
  };

  useEffect(() => { 刷().catch((e) => set讯(String(e.message || e))); }, []);

  const 改 = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const 提交 = async (body) => {
    set讯("提交中…");
    const r = await post("/下发", body);
    await 刷(r.意图键);
    set讯(`${r.票号} → ${r.状态}${r.幂等 ? "（幂等重放）" : r.顶替 ? `（世代顶替，作废 ${r.作废旧票}，第 ${r.意图世代} 世）` : `（第 ${r.意图世代} 世）`}`);
    return r;
  };

  const 灌基础 = async () => {
    await postFixture("入轨样例");
    await postFixture("分桶样例");
    set讯("已灌入轨 + 分桶样例，可直接下发");
  };

  const 异票再发 = async () => {
    const no = 新票号();
    setForm((f) => ({ ...f, 票号: no }));
    await 提交({ ...form, 票号: no });
  };

  const 双票并行 = async () => {
    const a = 新票号();
    const b = 新票号();
    set并行(`并行发出 ${a} 与 ${b} …`);
    const t0 = performance.now();
    const rs = await Promise.allSettled([
      post("/下发", { ...form, 票号: a }),
      post("/下发", { ...form, 票号: b }),
    ]);
    const ms = Math.round(performance.now() - t0);
    const ok = rs.map((r, i) => ({ 票: [a, b][i], 状态: r.status, 值: r.value, 因: r.reason }));
    await 刷();
    const c = await get("/核证");
    setCert(c);
    const live = c.存活票号 || [];
    set并行(
      `耗时 ${ms}ms｜` +
      ok.map((x) => (x.状态 === "fulfilled" ? `${x.票.slice(-6)}:${x.值?.状态}/gen${x.值?.意图世代}` : `${x.票.slice(-6)}:失败`)).join("　") +
      `｜核证存活 ${live.length} 张 ${live.length <= 1 ? "✔ 至多一张" : "✘ 双活!"}`
    );
  };

  const 跑自动化 = async () => {
    set讯("跑自动化压测中（同票重放 / 异票再发 / 双票并行 / 回读归属）…");
    const r = await post("/压测/意图去重", {});
    set自动(r);
    set讯(r.ok ? "自动化全部通过" : "自动化存在失败断言");
    await 刷();
  };

  return (
    <div>
      <h1 className="题">下发票 · 意图去重键收口</h1>
      <p className="述">
        意图键 = 版本 + 桶 + 业务世代，库内物化 <code>意图键 / 意图世代</code>。默认「世代顶替」：
        换票号重试会抬世代并作废旧票；部分唯一索引 + 事务保证同一意图至多一张有效已下发——
        <strong>不靠前端防抖</strong>，双票号并行也只允许一张存活，回读只归属新生效票。
      </p>

      <div className="行">
        <button onClick={() => 灌基础().catch((e) => set讯(String(e.message || e)))}>灌基础（入轨+分桶）</button>
        <button onClick={() => postFixture("下发样例").then(() => 刷()).then(() => set讯("已灌下发样例")).catch((e) => set讯(String(e.message || e)))}>灌下发样例</button>
        <button className="次" onClick={() => 刷().catch((e) => set讯(String(e.message || e)))}>刷新核证</button>
        <button className="次" onClick={() => 跑自动化().catch((e) => set讯(String(e.message || e)))}>跑自动化核证压测</button>
      </div>

      <div className="表单行">
        <input value={form.版本键} onChange={改("版本键")} placeholder="版本键" />
        <input value={form.桶键} onChange={改("桶键")} placeholder="桶键" />
        <input value={form.业务世代} onChange={改("业务世代")} placeholder="业务世代(可空)" style={{ minWidth: "9rem" }} />
        <input value={form.票号} onChange={改("票号")} placeholder="票号(空=同版本桶默认票)" />
        <button onClick={() => 提交(form).catch((e) => set讯(String(e.message || e)))}>下发 / 重放</button>
        <button className="次" onClick={() => 提交(form).catch((e) => set讯(String(e.message || e)))}>同票号重放一次</button>
        <button className="次" onClick={() => 异票再发().catch((e) => set讯(String(e.message || e)))}>异票号再发(顶替)</button>
        <button className="焰" onClick={() => 双票并行().catch((e) => set并行("并行异常：" + String(e.message || e)))}>双票号并行提交</button>
      </div>

      {讯 && <p className="讯">{讯}</p>}
      {并行 && <p className="并行结果">⇄ {并行}</p>}

      {自动 && (
        <div className={`核证卡 ${自动.ok ? "" : "坏"}`}>
          <strong>自动化压测 {自动.ok ? "全部通过 ✔" : "有失败 ✘"}</strong>
          <ul style={{ margin: "0.4rem 0 0", paddingLeft: "1.1rem" }}>
            {(自动.断言 || []).map((c, i) => (
              <li key={i} style={{ fontSize: "0.8rem", color: c.通过 ? "var(--通)" : "var(--败)" }}>
                {c.通过 ? "✔" : "✘"} {c.名}
              </li>
            ))}
          </ul>
        </div>
      )}

      <核证面板 cert={cert} />

      {!rows.length ? (
        <div className="空态">尚无下发票（先「灌基础」，再下发 / 异票再发 / 双票并行）</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>票号</th><th>意图键</th><th>意图世代</th><th>版本 / 桶</th>
              <th>状态</th><th>作废链路</th><th>下发时</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.票号}>
                <td style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.76rem" }}>{r.票号}</td>
                <td><span className="意图键码">{r.意图键 || "—"}</span></td>
                <td><span className="世代chip">第{r.意图世代 || r.世代 || 1}世</span></td>
                <td style={{ fontSize: "0.76rem" }}>{r.版本键}<br />{r.桶键}</td>
                <td>{状态徽章(r.状态)}</td>
                <td>
                  <div className="链路">
                    {r.前缘票号 && <div>顶替自<span className="箭">←</span>{r.前缘票号}</div>}
                    {r.顶替票号 && <div>被<span className="箭">→</span>{r.顶替票号} 顶替作废{r.作废时 ? ` · ${r.作废时}` : ""}</div>}
                    {!r.前缘票号 && !r.顶替票号 && <span>—</span>}
                  </div>
                </td>
                <td style={{ fontSize: "0.74rem" }}>{r.下发时}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
