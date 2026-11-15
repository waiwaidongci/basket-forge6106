import {useCallback,useEffect,useState} from 'react';
import {researchApi} from '../api/research';
import type {Run,Proposal,JointPlan} from '../types/domain';
export function useResearch(){const [runs,setRuns]=useState<Run[]>([]),[proposals,setProposals]=useState<Proposal[]>([]),[jointPlans,setJointPlans]=useState<JointPlan[]>([]),[error,setError]=useState('');const reload=useCallback(async()=>{try{const [r,p,j]=await Promise.all([researchApi.runs(),researchApi.proposals(),researchApi.jointPlans()]);setRuns(r);setProposals(p);setJointPlans(j);setError('');}catch(e){setError(e.message);}},[]);useEffect(()=>{reload();const timer=setInterval(reload,3000);return()=>clearInterval(timer);},[reload]);return {runs,proposals,jointPlans,error,reload};}
