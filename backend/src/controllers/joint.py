from fastapi import HTTPException
from src.services import joint_plans
from src.utils.csv_export import csv_response

ALLOCATION_HEADERS=['收据编号','是否命中','命中组数','组合规则','组合商品','本篮折后毛利','覆盖商品']
INVENTORY_HEADERS=['SKU','商品','供给上限','已占用','剩余','占用率']
CANDIDATE_HEADERS=['规则索引','组合商品','支持数','支持度','置信度','提升度','标价','成本','折扣后单次毛利','是否占用名额','命中次数','折后毛利']
UNCOVERED_HEADERS=['收据编号','商品SKU']

def _plan(db,id): return joint_plans.get_plan(db,id)

def export_allocations(db,id):
 plan=_plan(db,id);result=plan.result
 if plan.status not in ('optimal','feasible'): raise HTTPException(409,'联合测算尚未完成，暂无可下载分配')
 rows=[[a['reference'],'是' if a['covered'] else '否',a['hit_count'],','.join(str(i) for i in a['bundle_rule_indexes']),','.join('+'.join(x) for x in a['bundle_items']),a['gross_profit'],','.join(sorted({s for x in a['bundle_items'] for s in x}))] for a in result['allocations']]
 return csv_response(f'joint-plan-{id}-allocations.csv',rows,ALLOCATION_HEADERS)

def export_inventory(db,id):
 plan=_plan(db,id);result=plan.result
 if plan.status not in ('optimal','feasible'): raise HTTPException(409,'联合测算尚未完成，暂无可下载库存占用')
 rows=[[x['sku'],x['name'],x['cap'],x['used'],x['remaining'],x['utilization'] if x['utilization'] is not None else ''] for x in result['inventory']]
 return csv_response(f'joint-plan-{id}-inventory.csv',rows,INVENTORY_HEADERS)

def export_candidates(db,id):
 plan=_plan(db,id);result=plan.result
 if plan.status not in ('optimal','feasible'): raise HTTPException(409,'联合测算尚未完成，暂无可下载候选')
 rows=[[c['rule_index'],'+'.join(c['items']),c['support_count'],c['support'],c['confidence'],c['lift'],c['list_price'],c['list_cost'],c['gross_profit_per_hit'],'是' if c['selected'] else '否',c['hits'],c['gross_profit']] for c in result['candidates']]
 return csv_response(f'joint-plan-{id}-candidates.csv',rows,CANDIDATE_HEADERS)

def export_uncovered(db,id):
 plan=_plan(db,id);result=plan.result
 if plan.status not in ('optimal','feasible'): raise HTTPException(409,'联合测算尚未完成，暂无可下载未覆盖样本')
 rows=[[u['reference'],','.join(u['items'])] for u in result['uncovered']]
 return csv_response(f'joint-plan-{id}-uncovered.csv',rows,UNCOVERED_HEADERS)
