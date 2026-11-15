from sqlalchemy import select
from fastapi import HTTPException
from src.models import JointPlan,RuleRun,ReceiptBatch,Receipt,Product
from src.utils.serialize import row
from src.services import joint_optimizer
from src.services.audit import record
from src.constants.enums import RunStatus,PlanStatus

MAX_CANDIDATES=24

def list_plans(db):
 return [dict(row(p),result={}) for p in db.scalars(select(JointPlan).order_by(JointPlan.id.desc()))]

def get_plan(db,id):
 obj=db.get(JointPlan,id)
 if not obj: raise HTTPException(404,'联合测算不存在')
 return obj

def create_plan(db,data,user):
 run=db.get(RuleRun,data.run_id)
 if not run or run.status!=RunStatus.SUCCEEDED: raise HTTPException(422,'请选择已完成的规则研究')
 rules=run.result.get('rules',[])
 if not rules: raise HTTPException(422,'该研究没有可用于联合测算的规则')
 indexes=sorted(set(data.rule_indexes))
 if len(indexes)>MAX_CANDIDATES: raise HTTPException(422,f'候选组合最多 {MAX_CANDIDATES} 个')
 bad=[i for i in indexes if i>=len(rules)]
 if bad: raise HTTPException(422,'候选规则索引不存在: '+','.join(str(i) for i in bad))
 products={p.sku:p for p in db.scalars(select(Product))}
 candidates=[{'rule_index':i,'antecedent':rules[i]['antecedent'],'consequent':rules[i]['consequent'],'support_count':rules[i].get('support_count'),'support':rules[i].get('support'),'confidence':rules[i].get('confidence'),'lift':rules[i].get('lift')} for i in indexes]
 item_count=sum(len(c['antecedent'])+len(c['consequent']) for c in candidates)
 if item_count>200: raise HTTPException(422,'候选组合合计商品项过多，请减少候选')
 used_skus=sorted({sku for c in candidates for sku in list(c['antecedent'])+list(c['consequent'])})
 unknown=[sku for sku in used_skus if sku not in products]
 if unknown: raise HTTPException(422,'候选包含未知商品: '+','.join(unknown))
 missing=[sku for sku in used_skus if sku not in data.supply]
 if missing: raise HTTPException(422,'缺少商品供给上限: '+','.join(missing))
 extra=sorted(set(data.supply)-set(used_skus))
 basis={
  'run_id':run.id,'run_name':run.name,'batch_id':run.batch_id,
  'run_checksum':run.result.get('checksum'),'run_created_at':run.created_at.isoformat() if run.created_at else None,
  'thresholds':{'min_support':run.min_support,'min_confidence':run.min_confidence,'min_lift':run.min_lift,'max_size':run.max_size},
  'prices':{sku:{'name':products[sku].name,'price':products[sku].price,'cost':products[sku].cost} for sku in used_skus},
  'candidates':candidates,
 }
 obj=JointPlan(batch_id=run.batch_id,run_id=run.id,name=data.name,discount=data.discount,max_slots=data.max_slots,supply={sku:data.supply[sku] for sku in used_skus},rule_indexes=indexes,time_limit_ms=data.time_limit_ms,basis=basis,creator_id=user.id)
 db.add(obj);db.commit()
 from src.worker import run_joint_plan
 try: run_joint_plan.delay(obj.id)
 except Exception:
  obj.status=PlanStatus.FAILED;obj.error='队列不可用，请重新提交';db.commit();raise HTTPException(503,obj.error)
 record(db,user.email,'joint_plan.queue',{'plan_id':obj.id,'candidates':len(indexes),'discount':data.discount,'max_slots':data.max_slots})
 if extra: record(db,user.email,'joint_plan.extra_supply',{'plan_id':obj.id,'skus':extra})
 return dict(row(obj),extra_supply_ignored=extra)
