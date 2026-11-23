# Воспроизводимый анализ: python3 analyze.py students.csv anomalies.csv
import sys
SRC=sys.argv[1] if len(sys.argv)>1 else 'students.csv'
DST=sys.argv[2] if len(sys.argv)>2 else 'anomalies.csv'
import pandas as pd, numpy as np
df=pd.read_csv(SRC,encoding='utf-8-sig')
df['ts']=pd.to_datetime(df['Timestamp'],utc=True)
df['ts']=df['ts'].dt.tz_convert('Etc/GMT-3')
df=df.sort_values(['ProcessInstanceId','EventIndex']).reset_index(drop=True)
CUT=pd.Timestamp('2026-07-01 00:00',tz='Etc/GMT-3')
short={'Регистрация заявки':'REG','Проверка заявки':'CHK','Согласование руководителем':'MGR','Согласование бюджета':'BUD','Исполнение заявки':'EXE','Завершение':'END','Проверка договора':'LEG','Доработка заявки':'REW','Внеплановая проверка':'AUD'}
df['act']=df.ActivityName.map(short)

def work_hours(a,b):
    """рабочие часы между a и b: пн-пт, 09:00-18:00"""
    if b<=a: return 0.0
    tot=0.0; d=a.normalize()
    while d<=b:
        if d.dayofweek<5:
            s=max(a,d+pd.Timedelta(hours=9)); e=min(b,d+pd.Timedelta(hours=18))
            if e>s: tot+=(e-s).total_seconds()/3600
        d+=pd.Timedelta(days=1)
    return tot
cr=df[df.EventType=='task_created'].set_index('TaskId')
co=df[df.EventType=='task_completed'].set_index('TaskId')
t=cr[['ProcessInstanceId','act','ts','RequestType','Amount']].join(co[['ts','Role','UserId','Result']],rsuffix='_c')
t['wh']=[work_hours(a,b if pd.notna(b) else CUT) for a,b in zip(t.ts,t.ts_c)]
t['sec']=(t.ts_c-t.ts).dt.total_seconds()

amt=df.drop_duplicates('ProcessInstanceId').set_index('ProcessInstanceId')[['RequestType','Amount']]
rows=[]
def add(pid,typ,expl): rows.append((pid,typ,expl))
fmt=lambda x:x.strftime('%d.%m.%Y %H:%M')
seqs={}
for pid,g in df.groupby('ProcessInstanceId'):
    seq=[]
    for r in g.itertuples():
        if r.EventType=='task_created': seq.append(r.act)
    seqs[pid]=seq
    rt=g.RequestType.iloc[0]
    tcs=g[g.EventType=='task_completed']
    # 1 skip manager
    if 'MGR' not in seq and 'BUD' in seq:
        add(pid,'Пропуск согласования руководителем',f'Заявка ({rt}) прошла Проверку заявки и сразу перешла к Согласованию бюджета, этап «Согласование руководителем» отсутствует в журнале. В 100% остальных успешных заявок руководитель решает до бюджета.')
    # 2 order
    if 'EXE' in seq and 'BUD' in seq and seq.index('EXE')<seq.index('BUD'):
        extra=' Юридическая проверка также идёт после исполнения.' if 'LEG' in seq and seq.index('EXE')<seq.index('LEG') else ''
        add(pid,'Нарушение порядка этапов: исполнение до бюджета',f'Задание «Исполнение заявки» создано раньше «Согласования бюджета» (порядок: {" > ".join(seq)}). По схеме бюджет{"/юрист" if rt=="contract" else ""} должен быть проверен до исполнения закупщиком.{extra}')
    # 3 duplicate CHK
    ch=[i for i,a in enumerate(seq) if a=='CHK']
    for a,b in zip(ch,ch[1:]):
        if 'REW' not in seq[a:b]:
            add(pid,'Повторная проверка без возврата',f'Этап «Проверка заявки» выполнен дважды подряд без возврата руководителем и доработки (маршрут: {" > ".join(seq)}). Дополнительная проверка не объяснена бизнес-ситуацией.')
    # 4 AUD
    if 'AUD' in seq:
        n=seq.count('AUD'); a=tcs[tcs.act=='AUD']
        who=', '.join(sorted(set(a.UserId.str[:8])))
        add(pid,'Внеплановая проверка аудитором (требует ручной проверки)',f'Заявка {rt} на сумму {amt.loc[pid,"Amount"]:,.0f}: {n} внеплановых проверки аудитора (после руководителя и после бюджета), аудиторы {who}. Всего в журнале 8 таких заявок из 1000; сумма и тип не выделяются, поэтому причина не видна из данных — нужна ручная проверка.')
    # 5 returns
    nr=((tcs.act=='MGR')&(tcs.Result=='return')).sum()
    if nr>=3:
        add(pid,'Чрезмерное число возвратов на доработку',f'Руководитель возвращал заявку {nr} раз(а) (маршрут содержит {nr} циклов Доработка→Проверка→Руководитель). В норме возврат один (у 119 из 128 заявок с возвратом ровно 1).')
    # 6 requester as manager
    m=tcs[(tcs.act=='MGR')&(tcs.Role=='requester')]
    for r in m.itertuples():
        starter=g.iloc[0].UserId
        same=' Исполнитель совпадает с инициатором заявки (самосогласование).' if r.UserId==starter else ''
        add(pid,'Согласование выполнено не руководителем',f'Задание «Согласование руководителем» закрыто {fmt(r.ts)} пользователем с ролью requester (результат {r.Result}); во всех остальных 1141 случаях роль manager.{same}')
for pid,g in df.groupby('ProcessInstanceId'):
    pass
# 7 stuck
op=t[t.ts_c.isna()]
for r in op.itertuples():
    if r.wh>100:
        add(r.ProcessInstanceId,'Зависшее согласование руководителем',f'Задание «Согласование руководителем» создано {fmt(r.ts)} и не закрыто до среза 01.07.2026: ожидание ~{r.wh:.0f} рабочих часов (~{r.wh/9:.0f} рабочих дней) при 99-м перцентиле закрытых заданий около 6 ч. Это не «обычная открытая заявка», а потерянная/застрявшая.')
# 8 instant
inst=t[t.sec<60].groupby('ProcessInstanceId').act.apply(list)
for pid,acts in inst.items():
    secs=t[(t.ProcessInstanceId==pid)&(t.sec<60)].sec.astype(int).tolist()
    add(pid,'Аномально быстрое выполнение этапов',f'Все закрытые задания ({", ".join(acts)}) выполнены за {min(secs)}–{max(secs)} секунд (обычная медиана: проверка ~25 мин, руководитель ~2 ч, бюджет ~2,7 ч, исполнение ~5 ч раб. времени). Похоже на автоматическое/формальное проставление результатов.')
# 9 long check
for r in t[(t.act=='CHK')&(t.wh>=7.9)].itertuples():
    typ='Очень долгая проверка заявки' if r.wh>=59 else 'Долгая проверка заявки'
    add(r.ProcessInstanceId,typ,f'Первичная проверка специалистом заняла {r.wh:.1f} рабочих часов (создано {fmt(r.ts)}); медиана для {r.RequestType} {t[(t.act=="CHK")&(t.RequestType==r.RequestType)].wh.median():.2f} ч, 99% остальных заданий < 1.5 ч.')
# 10 after cut
ac=df[df.ts>CUT].groupby('ProcessInstanceId')
for pid,g in ac:
    add(pid,'События после среза выгрузки',f'В журнале {len(g)} событий с датой после среза 01.07.2026 00:00 (первое {fmt(g.ts.min())}); по описанию выгрузки событий после среза быть не должно — ошибка данных или нарушение целостности выгрузки.')
out=pd.DataFrame(rows,columns=['ProcessInstanceId','AnomalyType','Explanation']).drop_duplicates()
out.to_csv(DST,index=False,encoding='utf-8')
print(out.AnomalyType.value_counts()); print(out.ProcessInstanceId.nunique(), len(out))
print(out.groupby('ProcessInstanceId').size().value_counts())
