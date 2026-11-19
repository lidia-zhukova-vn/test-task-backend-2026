import logging
from collections import defaultdict
from typing import Iterator

from sqlalchemy import select
from abstract_customer import customer_to_row, lead_to_row
from amocrm import Customer as AmoCustomer, Lead as AmoLead, COMPANIES, CONTACTS, amocrm_get
from models import Customer, Lead, db

BATCH_SIZE = 1000

logger = logging.getLogger(__name__)


def rebuild_indices(ids_map: dict) -> None:
    for key in ('cust_by_amo', 'lead_by_amo',
                'cust_by_login', 'lead_by_login'):
        ids_map[key].clear()

    for cid, c in ids_map['rows_c'].items():
        if c.amo_id is not None:
            ids_map['cust_by_amo'][c.amo_id] = cid
        for extra in (c.merged_amo_ids or []):
            ids_map['cust_by_amo'][extra] = cid
        if c.login:
            ids_map['cust_by_login'][c.login].add(cid)

    for lid, l in ids_map['rows_l'].items():
        ids_map['lead_by_amo'][l.amo_id] = lid
        if l.login:
            ids_map['lead_by_login'][l.login].add(l.customer_id)


def preload() -> dict:
    # по одному select и построение всех строк в памяти
    # чтобы потом не надо было
    customers = db.session.scalars(select(Customer)).all()
    leads = db.session.scalars(select(Lead)).all()
    ids_map = {
        'rows_c': {c.id: c for c in customers},
        'rows_l': {l.id: l for l in leads},
        'cust_by_amo': {},
        'lead_by_amo': {},
        'cust_by_login': defaultdict(set),
        'lead_by_login': defaultdict(set),
    }
    rebuild_indices(ids_map)
    return ids_map


def check_unique(candidates) -> int | None:
    return next(iter(candidates)) if len(candidates) == 1 else None


def customers_action(ids_map, seen_c, seen_contacts, seen_companies, is_full):
    new_objs = []
    amo_customers: Iterator[AmoCustomer] = amocrm_get(AmoCustomer, _with=f"{COMPANIES},{CONTACTS}")

    for customer in amo_customers:
        if not is_full and customer.status_id == 111349:
            continue

        row = customer_to_row(customer)
        if not row['login']:
            continue
        if 'удалить' in (customer.name or '').lower():
            continue

        for item in row['contacts']:
            if isinstance(item, dict) and item.get('id') is not None:
                seen_contacts.add(item['id'])
        for item in row['companies']:
            if isinstance(item, dict) and item.get('id') is not None:
                seen_companies.add(item['id'])

        # 1) по amo_id CRM  2) по логину среди сделок
        target = ids_map['cust_by_amo'].get(customer.id)
        if target is None:
            target = check_unique(
                ids_map['lead_by_login'].get(row['login'], set()))

        if target is not None and target in ids_map['rows_c']:
            existing = ids_map['rows_c'][target]
            for k, v in row.items():
                if k != 'amo_id':
                    setattr(existing, k, v)
            seen_c.add(target)
        else:
            obj = Customer(**row)
            db.session.add(obj)
            new_objs.append(obj)
            if len(new_objs) >= BATCH_SIZE:
                db.session.flush()
                for o in new_objs:
                    ids_map['rows_c'][o.id] = o
                    seen_c.add(o.id)
                new_objs.clear()

    if new_objs:
        db.session.flush()
        for o in new_objs:
            ids_map['rows_c'][o.id] = o
            seen_c.add(o.id)

    db.session.commit()
    rebuild_indices(ids_map)


def orphan_customer(session, idx, orphan_by_login, login, name):
    if login and login in orphan_by_login:
        return idx['rows_c'][orphan_by_login[login]]
    obj = Customer(amo_id=None, login=login, name=name,
                   merged_amo_ids=[], contacts=[], companies=[])
    session.add(obj)
    session.flush([obj])  # id нужен прямо сейчас
    idx['rows_c'][obj.id] = obj
    if login:
        orphan_by_login[login] = obj.id
    return obj


def leads_action(ids_map, seen_c, seen_l, seen_contacts, is_full, merges):
    orphan_by_login: dict[str, int] = {}
    new_objs = []
    amo_leads: Iterator[AmoLead] = amocrm_get(AmoLead, _with=f"{CONTACTS}")

    for lead in amo_leads:
        if not is_full and lead.status_id == 143:
            continue
        login = (lead.custom_fields_values or {}).get('Логин', {}).get('value')
        for item in lead.contacts or []:
            if isinstance(item, dict) and item.get('id') is not None:
                seen_contacts.add(item['id'])

        existing_lid = ids_map['lead_by_amo'].get(lead.id)
        existing_cid = (ids_map['rows_l'][existing_lid].customer_id
                        if existing_lid is not None else None)
        login_cid = check_unique(
            ids_map['cust_by_login'].get(login, set())) if login else None

        if existing_cid is not None:
            target = existing_cid
            if login_cid is not None and login_cid != existing_cid:
                merges.append((existing_cid, login_cid))
            print(f"DEBUG lead {lead.id}: existing_cid={existing_cid} "
                  f"login_cid={login_cid} login={login!r} "
                  f"merge_queued={login_cid is not None and login_cid != existing_cid}")
        elif login_cid is not None:
            target = login_cid
        else:
            target = orphan_customer(
                db.session, ids_map, orphan_by_login, login, lead.name).id

        row = lead_to_row(lead, target)
        seen_c.add(target)
        seen_l.add(lead.id)

        if existing_lid is not None:
            existing = ids_map['rows_l'][existing_lid]
            for k, v in row.items():
                if k != 'amo_id':
                    setattr(existing, k, v)
        else:
            obj = Lead(**row)
            db.session.add(obj)
            new_objs.append(obj)
            if len(new_objs) >= BATCH_SIZE:
                db.session.flush()
                for o in new_objs:
                    ids_map['rows_l'][o.id] = o
                new_objs.clear()

    if new_objs:
        db.session.flush()
        for o in new_objs:
            ids_map['rows_l'][o.id] = o

    db.session.commit()
    rebuild_indices(ids_map)


def merge_jsonb(*lists):
    result, positions = [], {}
    for lst in lists:
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            iid = item.get('id')
            if iid is None:
                result.append(item)
            elif iid in positions:
                result[positions[iid]] = item
            else:
                positions[iid] = len(result)
                result.append(item)
    return result


def merges_action(ids_map, seen_c, merges):
    print(f"DEBUG merges_action called: merges={merges}, "
          f"rows_c keys={list(ids_map['rows_c'].keys())}")
    victim_to_target = {v: t for t, v in merges if t != v}
    print(f"DEBUG victim_to_target={victim_to_target}")

    def root(x):
        visited = set()
        while x in victim_to_target and x not in visited:
            visited.add(x)
            x = victim_to_target[x]
        return x

    groups: dict[int, set[int]] = defaultdict(set)
    for victim in victim_to_target:
        groups[root(victim)].add(victim)

    victims_to_delete = []

    for target_id, victim_ids in groups.items():
        target = ids_map['rows_c'].get(target_id)
        if target is None:
            continue
        for vid in victim_ids:
            if vid == target_id:
                continue
            victim = ids_map['rows_c'].get(vid)
            if victim is None:
                continue

            for lead in ids_map['rows_l'].values():
                if lead.customer_id == vid:
                    lead.customer_id = target_id

            if victim.amo_id is not None:
                if target.amo_id is None:
                    target.amo_id = victim.amo_id
                elif victim.amo_id != target.amo_id:
                    target.merged_amo_ids = list(target.merged_amo_ids or []) + [
                        victim.amo_id]
            for extra in (victim.merged_amo_ids or []):
                target.merged_amo_ids = list(target.merged_amo_ids or []) + [extra]

            for f in ('login', 'name'):
                if getattr(target, f) is None and getattr(victim, f) is not None:
                    setattr(target, f, getattr(victim, f))

            target.contacts = merge_jsonb(target.contacts, victim.contacts)
            target.companies = merge_jsonb(target.companies, victim.companies)

            seen_c.discard(vid)
            victims_to_delete.append(victim)

    db.session.flush()
    for victim in victims_to_delete:
        print(f"DEBUG deleting victim id={victim.id} amo_id={victim.amo_id}")
        db.session.delete(victim)

    db.session.commit()
    rebuild_indices(ids_map)


def filter_embedded(items, seen_ids):
    return [
        x for x in (items or [])
        if isinstance(x, dict)
           and x.get('id') in seen_ids
           and 'удалить' not in (x.get('name') or '').lower()
    ]


def phase_cleanup(idx, seen_c, seen_l, seen_contacts, seen_companies):
    for lid, lead in list(idx['rows_l'].items()):
        if lead.amo_id not in seen_l:
            db.session.delete(lead)
    db.session.flush()

    deleted_c = set()
    for cid, cust in list(idx['rows_c'].items()):
        if cid not in seen_c:
            db.session.delete(cust)
            deleted_c.add(cid)
    db.session.flush()

    for cid, cust in idx['rows_c'].items():
        if cid in deleted_c:
            continue
        if cust.contacts:
            cust.contacts = filter_embedded(cust.contacts, seen_contacts)
        if cust.companies:
            cust.companies = filter_embedded(cust.companies, seen_companies)


def collect_leads_customers(is_full=True):
    """
    Весь процесс collect_leads_customers () разделён на 4 фазы:
        1. preload() - единоразовое чтение индексов пользователей и сделок 2 SELECT-ами
        2. customers_action() - проходит по всем покупателям из AmoCRM и создаёт/обновить соответствующую
            строку Customer. Эффективно за счёт добавления новых пользователей батчами и flush после
        3. leads_action() - проходит по всем сделкам и привязывает каждую к покупателю по amo_id или логину,
            иначе сделка объявляется "сиротой"
        4. merges_action() - вливает victim покупателей в target покупателей по причине
            "сделка уже была привязана к target, но по логину указывает на victim"
    """
    db.session.expire_on_commit = False
    ids_map = preload()
    seen_c, seen_l = set(), set()
    seen_contacts, seen_companies = set(), set()
    merges: list[tuple[int, int]] = []

    try:
        customers_action(ids_map, seen_c, seen_contacts, seen_companies, is_full)
        leads_action(ids_map, seen_c, seen_l, seen_contacts, is_full, merges)
        if merges:
            merges_action(ids_map, seen_c, merges)
    except Exception:
        db.session.rollback()
        logger.exception(
            "Sync aborted; skipping cleanup "
            "(правило: при прерывании удаление не запускается)"
        )
        raise

    if is_full:
        try:
            phase_cleanup(ids_map, seen_c, seen_l, seen_contacts, seen_companies)
        except Exception:
            db.session.rollback()
            logger.exception("Cleanup failed")
            raise

    db.session.commit()
