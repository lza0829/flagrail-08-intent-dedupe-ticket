import React, { useEffect, useState } from "react";
import { get, postFixture } from "../api.js";

function 命运徽章(f) {
  let cls = "信";
  if (f === "回滚" || f === "失效") cls = "败";
  else if (f === "作废") cls = "警";
  else if (f === "下发") cls = "通";
  else if (f === "世代顶替") cls = "信";
  return <span className={`徽章 ${cls}`}>{f}</span>;
}

export default function 命运账() {
  const [rows, setRows] = useState([]);
  const [讯, set讯] = useState("");
  const 刷 = async () => setRows(await get("/命运账"));
  useEffect(() => { 刷().catch((e) => set讯(String(e.message || e))); }, []);
  return (
    <div>
      <h1 className="题">命运账</h1>
      <p className="述">
        下发 / 世代顶替 / 作废 / 回滚 / 失效都写命运条。意图换票重试时，旧票的「作废」与意图键上的
        「世代顶替」在此留链，和下发票页的作废链路、回读台的改挂归属互证。
      </p>
      <div className="行">
        <button className="焰" onClick={async () => { await postFixture("回滚样例"); await 刷(); set讯("已灌回滚样例"); }}>灌回滚样例</button>
        <button className="次" onClick={() => 刷().catch((e) => set讯(String(e.message || e)))}>刷新</button>
      </div>
      {讯 && <p className="讯">{讯}</p>}
      {!rows.length ? <div className="空态">尚无命运条（下发 / 异票顶替即会写入）</div> : (
        <table>
          <thead><tr><th>#</th><th>对象类型</th><th>对象键</th><th>命运</th><th>时刻</th><th>说明</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.编号}><td>{r.编号}</td><td>{r.对象类型}</td>
              <td style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: "0.76rem" }}>{r.对象键}</td>
              <td>{命运徽章(r.命运)}</td><td>{r.时刻}</td><td>{r.说明}</td></tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}
