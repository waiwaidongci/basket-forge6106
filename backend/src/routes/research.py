from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from src.config.database import get_db
from src.controllers.dependencies import current_user,writers,admins
from src.controllers.research import export_rules,export_proposals
from src.controllers import joint as joint_export
from src.services import runs,proposals,joint_plans
from src.schemas.run import RunInput
from src.schemas.proposal import ProposalInput,ReviewInput
from src.schemas.joint_plan import JointPlanInput
from src.utils.serialize import row
router=APIRouter(tags=['research'])
@router.get('/runs')
def all_runs(db:Session=Depends(get_db),user=Depends(current_user)): return runs.list_runs(db)
@router.post('/runs',status_code=201)
def queue(data:RunInput,db:Session=Depends(get_db),user=Depends(writers)): return runs.create_run(db,data,user)
@router.get('/runs/{id}')
def detail(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return row(runs.get_run(db,id))
@router.get('/runs/{id}/export')
def export(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return export_rules(db,id)
@router.get('/proposals')
def all_proposals(db:Session=Depends(get_db),user=Depends(current_user)): return proposals.list_proposals(db)
@router.post('/proposals',status_code=201)
def add(data:ProposalInput,db:Session=Depends(get_db),user=Depends(writers)): return proposals.create_proposal(db,data,user)
@router.patch('/proposals/{id}/review')
def review(id:int,data:ReviewInput,db:Session=Depends(get_db),user=Depends(admins)): return proposals.review_proposal(db,id,data,user)
@router.get('/proposals-export')
def export_all(db:Session=Depends(get_db),user=Depends(current_user)): return export_proposals(db)

@router.get('/joint-plans')
def all_joint_plans(db:Session=Depends(get_db),user=Depends(current_user)): return joint_plans.list_plans(db)
@router.post('/joint-plans',status_code=201)
def add_joint_plan(data:JointPlanInput,db:Session=Depends(get_db),user=Depends(writers)): return joint_plans.create_plan(db,data,user)
@router.get('/joint-plans/{id}')
def joint_plan(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return row(joint_plans.get_plan(db,id))
@router.get('/joint-plans/{id}/export/allocations')
def joint_allocations(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return joint_export.export_allocations(db,id)
@router.get('/joint-plans/{id}/export/inventory')
def joint_inventory(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return joint_export.export_inventory(db,id)
@router.get('/joint-plans/{id}/export/candidates')
def joint_candidates(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return joint_export.export_candidates(db,id)
@router.get('/joint-plans/{id}/export/uncovered')
def joint_uncovered(id:int,db:Session=Depends(get_db),user=Depends(current_user)): return joint_export.export_uncovered(db,id)
