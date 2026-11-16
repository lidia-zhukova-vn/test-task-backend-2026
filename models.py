from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, MetaData,
    Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB


naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class Customer(db.Model):
    """
    покупатель
    `amo_id`            primary CRM-id, UNIQUE (NULL - для «осиротевших»)
    `merged_amo_ids`    CRM-id, «впитанные» при мердже; по ним тоже поиск,
                        чтобы не создавать дубли при следующем прогоне
    `login`, `name`     вынесены из jsonb и проиндексированы: их ищут
                        и ночной прогон, и другие сервисы
    `api_value`, `contacts`, `companies`    сырой ответ AmoCRM (jsonb)"""

    __tablename__ = "customer"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    amo_id = Column(BigInteger, unique=True, nullable=True)
    merged_amo_ids = Column(JSONB, nullable=False, default=list,
                            server_default=text("'[]'::jsonb"))
    login = Column(Text, nullable=True)
    name = Column(Text, nullable=True)
    api_value = Column(JSONB)
    contacts = Column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    companies = Column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        Index("ix_customer_login", "login"),
        Index("ix_customer_name", "name"),
        Index("ix_customer_merged_amo_ids", "merged_amo_ids",
              postgresql_using="gin"),
    )


class Lead(db.Model):
    __tablename__ = "lead"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    amo_id = Column(BigInteger, unique=True, nullable=False)
    customer_id = Column(BigInteger,
                         ForeignKey("customer.id", ondelete="RESTRICT"),
                         nullable=False)
    login = Column(Text, nullable=True)
    name = Column(Text, nullable=True)
    api_value = Column(JSONB)
    contacts = Column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        Index("ix_lead_login", "login"),
        Index("ix_lead_customer", "customer_id"),
    )
