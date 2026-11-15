from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,JSON,Float,Integer,ForeignKey,DateTime
from datetime import datetime,timezone
from src.config.database import Base
class JointPlan(Base):
 __tablename__='joint_plans'
 id:Mapped[int]=mapped_column(primary_key=True)
 batch_id:Mapped[int]=mapped_column(ForeignKey('receipt_batches.id'))
 run_id:Mapped[int]=mapped_column(ForeignKey('rule_runs.id'))
 name:Mapped[str]=mapped_column(String(100))
 status:Mapped[str]=mapped_column(String(20),default='queued')
 discount:Mapped[float]=mapped_column(Float)
 max_slots:Mapped[int]=mapped_column(Integer,default=8)
 supply:Mapped[dict]=mapped_column(JSON,default=dict)
 rule_indexes:Mapped[list]=mapped_column(JSON,default=list)
 time_limit_ms:Mapped[int]=mapped_column(Integer,default=25000)
 basis:Mapped[dict]=mapped_column(JSON,default=dict)
 result:Mapped[dict]=mapped_column(JSON,default=dict)
 error:Mapped[str|None]=mapped_column(String(1000),nullable=True)
 creator_id:Mapped[int]=mapped_column(ForeignKey('users.id'))
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
