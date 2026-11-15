import {RUN_STATUS,PROPOSAL_STATUS,PLAN_STATUS} from '../../constants/enums';
export function Badge({status}:{status:string}){return <span className={'badge '+status}>{RUN_STATUS[status]||PROPOSAL_STATUS[status]||PLAN_STATUS[status]||status}</span>}
