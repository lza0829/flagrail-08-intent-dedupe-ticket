import React, { useEffect, useState } from "react";
import { get, post, postFixture } from "../api.js";

export default function 回读台() {
  const [rows, setRows] = useState([]);
  const [讯, set讯] = useState("");
  const [form, setForm] = useState({ 票号: "", 边缘键: "edge-a", 所见版本键: "" });
  const 刷 = async () => setRows(await get("/回读证"));
  useEffect(() => { 刷().catch((e) => set讯(String(e.message || e))); }, []);

  const 改 = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const 提交 = async () => {
    set讯("提交回读…");
    const r = await post("/回读", form);
    await 刷();
    set讯(
      r.改挂
        ? `旧票号已作废：回读改挂到唯一存活票 ${r.归属票号}（第 ${r.意图世代} 世），原报 ${r.原报票号}`
        : `回读归属 ${r.归属票号}（第 ${r.意图世代} 世）· 结果 ${r.结果}`
    );
  };

  return (
    <div>
      <h1 className="题">回读台 · 归属唯一票</h1>
      <p className="述">
        边缘对下发票的确认。所见版本与票面不一致 → 记「偏差」。世代顶替后，对已作废旧票号的回读会
        <strong>自动改挂到当前唯一存活新票</strong>（「票号 / 归属票号」只挂新生效票，「原报票号」留痕），
        因此同一意图的回读归属唯一、不跟错票。
      </p>

      <div className="表单行">
        <input value={form.票号} onChange={改("票号")} placeholder="票号（填旧票号也会改挂）" style={{ minWidth: "16rem" }} />
        <input value={form.边缘键} onChange={改("边缘键")} placeholder="边缘键" style={{ minWidth: "8rem" }} />
        <input value={form.所见版本键} onChange={改("所见版本键")} placeholder="所见版本键(空=票面版本)" style={{ minWidth: "14rem" }} />
        <button onClick={() => 提交().catch((e) => set讯(String(e.message || e)))}>记回读</button>
      </div>

      <div className="行">
        <button onClick={async () => { await postFixture("回读样例"); await 刷(); set讯("已灌回读样例"); }}>灌回读样例</button>
        <button className="次" onClick={() => 刷().catch((e) => set讯(String(e.message || e)))}>刷新</button>
      </div>
      {讯 && <p className="讯">{讯}</p>}

      {!rows.length ? (
        <div className="空态">尚无回读证</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>证号</th><th>归属票号(规范)</th><th>原报票号</th><th>边缘</th>
              <th>所见版本</th><th>意图世代</th><th>改挂</th><th>结果</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.证号}>
                <td style={{ fontSize: "0.74rem" }}>{r.证号}</td>
                <td style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.76rem" }}>
                  {r.归属票号 || r.票号}
                </td>
                <td style={{ fontSize: "0.74rem", color: "var(--淡)" }}>
                  {r.原报票号 && r.原报票号 !== (r.归属票号 || r.票号) ? r.原报票号 : "—"}
                </td>
                <td>{r.边缘键}</td>
                <td>{r.所见版本键}</td>
                <td>{r.意图世代 ? <span className="世代chip">第{r.意图世代}世</span> : "—"}</td>
                <td>{r.改挂 ? <span className="徽章 警">改挂</span> : <span className="徽章 信">直挂</span>}</td>
                <td><span className={"徽章 " + (r.结果 === "确认" ? "通" : "败")}>{r.结果}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
