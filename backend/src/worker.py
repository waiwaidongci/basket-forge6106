import json,time
from pathlib import Path
from celery import Celery
from sqlalchemy import select
from src.config.settings import REDIS_URL,ARTIFACT_DIR
from src.config.database import SessionLocal
from src.models import RuleRun,Receipt,Product,JointPlan
from src.services.apriori import mine_rules
from src.services import joint_optimizer
from src.services.audit import record
from src.constants.enums import RunStatus,PlanStatus
app=Celery('basket_forge',broker=REDIS_URL,backend=REDIS_URL)
app.conf.update(task_track_started=True,worker_prefetch_multiplier=1,task_acks_late=True)

def _prices_from_basis(basis):
 return {sku:{'price':float(v['price']),'cost':float(v['cost']),'name':v.get('name')} for sku,v in basis['prices'].items()}

@app.task(name='basket.analyze',soft_time_limit=120,time_limit=150)
def analyze_run(id):
 with SessionLocal() as db:
  run=db.get(RuleRun,id)
  if not run or run.status==RunStatus.SUCCEEDED: return
  run.status=RunStatus.RUNNING;db.commit()
  try:
   baskets=list(db.scalars(select(Receipt.items).where(Receipt.batch_id==run.batch_id).order_by(Receipt.id)))
   products={p.sku:{'price':p.price,'cost':p.cost} for p in db.scalars(select(Product))}
   result=mine_rules(baskets,products,run.min_support,run.min_confidence,run.min_lift,run.max_size)
   Path(ARTIFACT_DIR).mkdir(parents=True,exist_ok=True)
   Path(ARTIFACT_DIR,f'run-{id}.json').write_text(json.dumps(result,ensure_ascii=False))
   run.result=result;run.status=RunStatus.SUCCEEDED;run.error=None;db.commit();record(db,'worker','run.succeeded',{'run_id':id,'rules':len(result['rules'])})
  except Exception as e:
   db.rollback();run=db.get(RuleRun,id);run.status=RunStatus.FAILED;run.error=str(e)[:1000];db.commit();record(db,'worker','run.failed',{'run_id':id,'error':run.error})

@app.task(name='basket.joint_plan',soft_time_limit=70,time_limit=85)
def run_joint_plan(id):
 with SessionLocal() as db:
  plan=db.get(JointPlan,id)
  if not plan or plan.status in (PlanStatus.OPTIMAL,PlanStatus.FEASIBLE): return
  plan.status=PlanStatus.RUNNING;db.commit()
  try:
   run=db.get(RuleRun,plan.run_id)
   if not run or run.status!=RunStatus.SUCCEEDED: raise ValueError('规则研究已不可用')
   rules=run.result.get('rules',[])
   indexes=list(plan.rule_indexes)
   if any(i>=len(rules) for i in indexes): raise ValueError('候选规则索引已变化')
   snapshot=plan.basis.get('candidates')
   if not snapshot: raise ValueError('联合测算缺少冻结的候选快照')
   for frozen in snapshot:
    live=rules[frozen['rule_index']]
    if frozen['antecedent']!=live['antecedent'] or frozen['consequent']!=live['consequent']:
     raise ValueError('候选规则与冻结快照不一致，拒绝计算')
   candidates=[{'rule_index':c['rule_index'],'antecedent':c['antecedent'],'consequent':c['consequent'],'support_count':c.get('support_count'),'support':c.get('support'),'confidence':c.get('confidence'),'lift':c.get('lift')} for c in snapshot]
   receipts=[{'reference':r.reference,'items':r.items} for r in db.scalars(select(Receipt).where(Receipt.batch_id==plan.batch_id).order_by(Receipt.id))]
   basis=plan.basis;products=_prices_from_basis(basis)
   started=time.monotonic()
   result=joint_optimizer.optimize(receipts,candidates,products,plan.discount,plan.max_slots,plan.supply,time_limit=plan.time_limit_ms/1000)
   audit=result.get('audit') or joint_optimizer.audit_result(result,receipts,products,plan.supply,plan.discount,plan.max_slots)
   fingerprint=joint_optimizer.input_fingerprint(receipts,candidates,products,plan.discount,plan.max_slots,plan.supply)
   result['fingerprint']=fingerprint
   result['basis']=basis
   result['elapsed_ms']=round((time.monotonic()-started)*1000,1)
   Path(ARTIFACT_DIR).mkdir(parents=True,exist_ok=True)
   Path(ARTIFACT_DIR,f'joint-plan-{id}.json').write_text(json.dumps(result,ensure_ascii=False))
   plan.result=result
   plan.status=PlanStatus.OPTIMAL if result['optimal'] else PlanStatus.FEASIBLE
   plan.error=None;db.commit()
   record(db,'worker','joint_plan.succeeded',{'plan_id':id,'status':plan.status,'profit':result['summary']['gross_profit'],'bound':result['summary']['upper_bound'],'audit':audit['passed']})
  except Exception as e:
   db.rollback();plan=db.get(JointPlan,id)
   plan.status=PlanStatus.FAILED;plan.error=str(e)[:1000];db.commit();record(db,'worker','joint_plan.failed',{'plan_id':id,'error':plan.error})
