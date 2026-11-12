"""Capacity is confirmed net availability, never attendance or task duration."""

def confirmation_scope(person):
    kind = (person or {}).get('person_type')
    if kind == 'employee':
        return 'employee_excluded'
    return 'non_employee' if kind in ('vendor', 'external') else 'identity_unknown'


def capacity_summary(data, project_id, week_start=None):
    people=[]
    identities = {p['id']: p for p in data.get('people', [])}
    for row in data.get('resource_weeks',[]):
        if row['project_id']!=project_id or (week_start and row['week_start']!=week_start): continue
        values=[row.get(k) for k in ('net_available_hours','project_hours','operations_hours','buffer_hours')]
        known=all(v is not None for v in values) and bool(row.get('confirmed_at'))
        if known:
            c,p,o,b=values; remaining=c-p-o-b
            status='overallocated' if p+o>c else 'buffer_conflict' if remaining<0 else 'available'
        else: remaining=None;status='pending'
        scope = confirmation_scope(identities.get(row['person_id']))
        people.append(dict(row,remaining_hours=remaining,status=status,
                           confirmation_scope=scope, confirmation_required=scope == 'non_employee'))
    return {'project_id':project_id,'week_start':week_start,'people':people,'unit':'hours','basis':'net capacity includes project and related operations; leave already excluded is not deducted again'}
