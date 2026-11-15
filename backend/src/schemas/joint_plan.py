from pydantic import BaseModel,Field,model_validator
from typing import Annotated
class JointPlanInput(BaseModel):
 name:str=Field(min_length=1,max_length=100)
 run_id:int=Field(gt=0)
 rule_indexes:list[int]=Field(min_length=1,max_length=24)
 discount:float=Field(ge=0,lt=1,allow_inf_nan=False)
 max_slots:int=Field(ge=1,le=8)
 supply:dict[Annotated[str,Field(min_length=1,max_length=30)],Annotated[int,Field(ge=0,le=1000000)]]=Field(min_length=1,max_length=200)
 time_limit_ms:int=Field(ge=1000,le=60000,default=25000)
 @model_validator(mode='after')
 def check_rules(self):
  if len(set(self.rule_indexes))!=len(self.rule_indexes): raise ValueError('候选规则不得重复')
  if any(x<0 for x in self.rule_indexes): raise ValueError('候选规则索引不合法')
  if any(v<0 for v in self.supply.values()): raise ValueError('供给上限必须是非负整数')
  return self
