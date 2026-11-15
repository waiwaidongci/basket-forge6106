import {useEffect,useState} from 'react';
import type {Run,JointPlan,Product,User} from '../types/domain';
import {researchApi} from '../api/research';
import {PageTitle} from '../components/common/PageTitle';
import {Table} from '../components/common/Table';
import {Badge} from '../components/common/Badge';
import {Empty} from '../components/common/Empty';
import {Notice} from '../components/common/Notice';
import {useAsync} from '../hooks/useAsync';
import {names,pct,decimal} from '../utils/format';
import {PLAN_REASON} from '../constants/enums';

const MAX_CANDIDATES=24;

export function Joint({runs,jointPlans,products,user,reload}:{runs:Run[];jointPlans:JointPlan[];products:Product[];user:User;reload:()=>Promise<any>}){
 const {busy,error,message,act}=useAsync();
 const completed=runs.filter(r=>r.status==='succeeded'&&(r.result.rules?.length||0)>0);
 const [runId,setRunId]=useState(0),[name,setName]=useState('联合陈列测算'),[discount,setDiscount]=useState(.1),[slots,setSlots]=useState(8),[limit,setLimit]=useState(25000),[ruleIndexes,setRuleIndexes]=useState<number[]>([]),[supply,setSupply]=useState<Record<string,number>>({});
 const run=completed.find(r=>r.id===(runId||completed[0]?.id));
 const rules=run?.result.rules||[];
 const selected=ruleIndexes.filter(i=>i<rules.length);
 const skus=Array.from(new Set(selected.flatMap(i=>[...(rules[i]?.antecedent||[]),...(rules[i]?.consequent||[])]))).sort();
 const [expanded,setExpanded]=useState<number|null>(null);
 const [details,setDetails]=useState<Record<number,JointPlan>>({});
 const completedPlans=jointPlans.filter(p=>p.status==='optimal'||p.status==='feasible');
 const detailKey=completedPlans.map(p=>`${p.id}:${p.status}`).join(',');
 useEffect(()=>{let cancelled=false;Promise.all(completedPlans.filter(p=>!details[p.id]).map(p=>researchApi.jointPlan(p.id).then(d=>[p.id,d] as const).catch(()=>null))).then(rows=>{if(cancelled||!rows.some(Boolean))return;setDetails(prev=>{const next={...prev};rows.forEach(r=>{if(r)next[r[0]]=r[1]});return next})});return()=>{cancelled=true}}, [detailKey]);
 function toggleRule(index:number){setRuleIndexes(current=>current.includes(index)?current.filter(i=>i!==index):current.length>=MAX_CANDIDATES?current:[...current,index].sort((a,b)=>a-b))}
 function supplyValue(sku:string){return supply[sku]??1000}
 return <><PageTitle eyebrow="07 / JOINT PILOT" title="一张收据，可以命中不重叠的组合。" description="从已完成研究挑选最多 24 个候选，设置折扣、最多 8 套陈列名额与各商品供给上限。每张收据每种商品只分配一次，以折后总毛利最大为目标。"/><Notice error={error} message={message}/><div className="joint-layout"><form className="editor" onSubmit={e=>{e.preventDefault();act(async()=>{const payload={name,run_id:run?.id,rule_indexes:selected,discount,max_slots:slots,supply:Object.fromEntries(skus.map(s=>[s,supplyValue(s)])),time_limit_ms:limit};await researchApi.queueJoint(payload);await reload()},'联合测算已入队，worker 完成后自动刷新')}}><h2>联合测算参数</h2><fieldset disabled={user.role==='viewer'||busy}>
 <label>测算名称<input value={name} onChange={e=>setName(e.target.value)} required maxLength={100}/></label>
 <label>已完成研究<select value={run?.id||''} onChange={e=>{setRunId(Number(e.target.value));setRuleIndexes([])}}>{completed.length?completed.map(r=><option key={r.id} value={r.id}>{r.name} / {r.result.rules?.length||0} 条规则</option>):<option value="">暂无已完成研究</option>}</select></label>
 <div className="form-grid"><label>全场折扣<input type="number" step=".01" min="0" max=".99" value={discount} onChange={e=>setDiscount(+e.target.value)}/></label><label>陈列名额（最多 8）<input type="number" step="1" min="1" max="8" value={slots} onChange={e=>setSlots(+e.target.value)}/></label><label>时间上限（毫秒，≤60000）<input type="number" step="1000" min="1000" max="60000" value={limit} onChange={e=>setLimit(+e.target.value)}/></label><label>已选候选 / 上限<input readOnly value={`${selected.length} / ${MAX_CANDIDATES}`}/></label></div>
 <div className="section-heading"><h2>候选规则</h2><span>每张收据可同时命中互不重叠的组合</span></div>
 {rules.length?<div className="joint-rules">{rules.map((r,index)=><label key={index} className={'joint-rule'+(ruleIndexes.includes(index)?' selected':'')}><input type="checkbox" checked={ruleIndexes.includes(index)} onChange={()=>toggleRule(index)}/><span><strong>{names(r.antecedent,products)} → {names(r.consequent,products)}</strong><small>支持数 {r.support_count} · 提升度 {decimal(r.lift)}× · 毛利率 {pct(r.margin)}</small></span></label>)}</div>:<Empty title="先完成一份规则研究" help="联合测算从已完成研究的规则中挑选候选。"/>}
 {skus.length>0&&<><div className="section-heading"><h2>商品供给上限</h2><span>仅列出所选候选涉及的 {skus.length} 个 SKU</span></div><div className="form-grid joint-supply">{skus.map(sku=><label key={sku}>{products.find(p=>p.sku===sku)?.name||sku}<input type="number" min="0" step="1" value={supplyValue(sku)} onChange={e=>setSupply({...supply,[sku]:Math.max(0,Math.floor(+e.target.value||0))})}/></label>)}</div></>}
 <button className="primary" disabled={!completed.length||!selected.length||busy}>{busy?'正在提交…':'提交联合测算 →'}</button>
 </fieldset><p>计算在独立 worker 的单核内完成；超时会返回可行方案、可核验上界与差距，未证最优会在页面明示。</p></form>
 <aside className="method-sheet"><small>MODEL CARD / JOINT</small><h2>一次分配，<br/>不重复计毛利。</h2><ol><li><b>每收据每商品一次</b><p>同一张收据可命中多个组合，但商品不得跨组合重复占用。</p></li><li><b>陈列名额与供给</b><p>最多 8 套组合获得陈列名额；每个 SKU 的命中总数受供给上限约束。</p></li><li><b>上界与差距</b><p>同时给出名额对偶、逐篮冲突松弛与精确选择松弛三个上界，取最小值向上取整到分。</p></li></ol><p className="fine">折扣作用于组合标价；毛利未扣损耗与陈列成本。候选与价格来自冻结的研究快照。</p></aside></div>
 <div className="section-heading"><h2>联合测算记录</h2><span>每 3 秒刷新</span></div>
 {jointPlans.length?<div className="joint-plans">{jointPlans.map(plan=>{const result=((details[plan.id]||plan).result||{}) as any;const summary=result?.summary;const isOpen=expanded===plan.id;return <article key={plan.id} className="joint-plan"><header><div><small>JOINT / {String(plan.id).padStart(3,'0')}</small><h2>{plan.name}</h2><p className="fine">研究 {plan.run_id} · 候选 {plan.rule_indexes.length} 个 · 折扣 {pct(plan.discount)} · 名额 {plan.max_slots} · 时限 {plan.time_limit_ms}ms</p></div><div className="joint-plan-status"><Badge status={plan.status}/>{plan.error&&<small className="error-text">{plan.error}</small>}{summary&&<><b>{decimal(summary.gross_profit)}</b><small>折后总毛利</small></>}</div></header>
 {summary?<><div className="joint-kpis"><div><small>覆盖收据</small><b>{summary.covered_receipt_count}/{summary.receipt_count}</b></div><div><small>命中组合</small><b>{summary.hit_count}</b></div><div><small>占用名额</small><b>{summary.selected_bundle_count}/{summary.max_slots}</b></div><div><small>可核验上界</small><b>{decimal(summary.upper_bound)}</b></div><div><small>差距</small><b>{decimal(summary.gap)}</b><span>{pct(summary.gap_pct)}</span></div><div><small>状态</small><b className={summary.optimal?'ok-text':'warn-text'}>{summary.optimal?'已证最优':'未证最优'}</b><span>{PLAN_REASON[summary.optimality_reason]||summary.optimality_reason}</span></div></div>
 <div className="joint-actions"><button onClick={()=>setExpanded(isOpen?null:plan.id)}>{isOpen?'收起明细':'展开明细'}</button>{['allocations','inventory','candidates','uncovered'].map(kind=><button key={kind} disabled={plan.status!=='optimal'&&plan.status!=='feasible'} onClick={()=>act(()=>researchApi.exportJoint(plan.id,kind as any),'已导出 '+kind)}>下载 {{allocations:'逐篮分配',inventory:'库存占用',candidates:'候选汇总',uncovered:'未覆盖样本'}[kind]} CSV ↗</button>)}</div>
 {isOpen&&<div className="joint-detail"><section><h3>逐篮分配</h3><Table headers={['收据','命中','组合','本篮折后毛利']}>{result.allocations.slice(0,200).map((a:any)=><tr key={a.reference}><td className="mono">{a.reference}</td><td>{a.covered?'是':'—'}</td><td>{a.bundle_rule_indexes.length?a.bundle_rule_indexes.map((i:number)=><small key={i}>规则 #{i}: {names(result.candidates.find((c:any)=>c.rule_index===i)?.items||[],products)}</small>):'—'}</td><td>{decimal(a.gross_profit)}</td></tr>)}</Table>{result.allocations.length>200&&<p className="fine">页面展示前 200 张，完整分配请下载 CSV。</p>}</section>
 <section><h3>库存占用</h3><Table headers={['SKU','商品','上限','已占用','剩余','占用率']}>{result.inventory.map((x:any)=><tr key={x.sku}><td className="mono">{x.sku}</td><td>{x.name||x.sku}</td><td>{x.cap}</td><td>{x.used}</td><td>{x.remaining}</td><td>{x.utilization===null?'—':pct(x.utilization)}</td></tr>)}</Table></section>
 <section><h3>候选与上界</h3><Table headers={['规则','组合','单次折后毛利','名额','命中','折后毛利']}>{result.candidates.map((c:any)=><tr key={c.rule_index} className={c.selected?'selected':''}><td>#{c.rule_index}</td><td>{names(c.items,products)}</td><td>{decimal(c.gross_profit_per_hit)}</td><td>{c.selected?'占用':'—'}</td><td>{c.hits}</td><td>{decimal(c.gross_profit)}</td></tr>)}</Table><p className="fine">上界构成：{result.bound.note} 当前上界 {decimal(result.bound.upper_bound)}（名额对偶 {decimal(result.bound.slot_bound)}；逐篮冲突 {result.bound.conflict_bound===null?'未完成':decimal(result.bound.conflict_bound)}；精确选择 {result.bound.exact_selection_bound===null?'未完成':decimal(result.bound.exact_selection_bound)}）。</p></section>
 <section><h3>未覆盖样本</h3>{result.uncovered.length?<Table headers={['收据','商品']}>{result.uncovered.slice(0,100).map((u:any)=><tr key={u.reference}><td className="mono">{u.reference}</td><td>{names(u.items,products)}</td></tr>)}</Table>:<p className="fine">全部收据均至少命中一个组合。</p>}{result.uncovered.length>100&&<p className="fine">页面展示前 100 条，完整清单请下载 CSV。</p>}</section>
 <section><h3>可核验性</h3><div className="joint-verify"><span>输入指纹 <code>{result.fingerprint?.slice(0,24)}…</code></span><span>价格依据：研究 #{result.basis?.run_id||plan.run_id} 完成时的商品价格与成本</span><span>复核：{result.audit?.passed?'全部检查通过':(result.audit?.checks||[]).filter((c:any)=>!c.passed).map((c:any)=>c.check).join('、')}</span><span>算法：{result.diagnostics?.algorithm} · 变体 {result.diagnostics?.variant} · 用时 {result.summary?.elapsed_ms}ms</span></div></section></div>}</>:<p className="fine">{plan.status==='failed'?plan.error:'worker 正在测算，页面每 3 秒刷新。'}</p>}
 </article>})}</div>:<Empty title="还没有联合测算" help="先在规则实验完成研究，再在此挑选最多 24 个候选。"/>}
 </>}
