"""Run the task 3 benchmark and final accounts from the bundled market archive."""
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parent
TASK=ROOT/'task3'

def verify(actual,expected,keys):
    with actual.open() as f,expected.open() as ref:
        rows=list(csv.DictReader(f))
        reference=list(csv.DictReader(ref))
    if len(rows)!=len(reference) or not rows or rows[0].keys()!=reference[0].keys():
        raise ValueError(f'Incomplete table: {actual.name}')
    for row,control in zip(rows,reference):
        for k,v in row.items():
            if k in keys:
                equal=v==control[k]
            else:
                equal=math.isclose(float(v),float(control[k]),rel_tol=1e-10,abs_tol=1e-9)
            if not equal:
                raise ValueError(f'{actual.name}: {k} {v} != {control[k]}')

def main():
    with TemporaryDirectory(prefix='btc-task3-') as tmp:
        engine=Path(tmp)/'replay'
        shutil.copytree(TASK/'replay',engine)
        for archive in sorted((TASK/'data').glob('*.zip')):
            with ZipFile(archive) as z:
                z.extractall(engine)
        cal=engine/'calibration'
        paths=[TASK,engine/'execution_4pct',cal,cal/'trend_original',engine]
        env=dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,paths)),PYTHONDONTWRITEBYTECODE='1')
        def run(label,*args):
            print(label,flush=True)
            subprocess.run([sys.executable,'-B',*args],cwd=engine,env=env,check=True,stdout=subprocess.DEVNULL)
        run('1/5  Стрэддл внутри диапазона и три бенчмарка', 'research.py','--save')
        with (engine/'results/run.json').open() as stream:
            result=json.load(stream)['result']
        reference=json.loads((TASK/'expected/baseline.json').read_text())
        if result['cycles']!=reference['cycles'] or set(result['books'])!=set(reference['books']):
            raise ValueError('Baseline accounts or cycle count differ from the report')
        for book,values in reference['books'].items():
            for metric,value in values.items():
                if not math.isclose(result['books'][book][metric],value,rel_tol=1e-10,abs_tol=1e-9):
                    raise ValueError(f'Baseline mismatch: {book}, {metric}')
        run('2/5  Пакет STRD, грид и дельта-хедж','calibration/option_study.py')
        run('3/5  Проверка лимитных сделок самостоятельного грида','run_grid.py')
        run('4/5  Тренд и пропуск лучшей кампании','calibration/trend_check.py')
        run('5/5  Общий счёт 50/50 и сверка результатов','-m','core.combined')
        run('Сводная таблица','calibration/export_comparison.py')
        verify(cal/'comparison.csv',TASK/'expected/comparison.csv',{'account','year'})
        verify(cal/'comparison_daily.csv',TASK/'expected/comparison_daily.csv',{'date_utc'})
        out=ROOT/'results'
        out.mkdir(exist_ok=True)
        for name in ('comparison.csv','comparison_daily.csv','grid_walk.json','option_results.json','trend_results.json','combined_results.json'):
            shutil.copy2(cal/name,out/name)
        for name in ('run.json','equity.csv'):
            shutil.copy2(engine/'results'/name,out/('task3_'+name))
        print('Проверено: пять счетов, годовые результаты и 1049 дневных отметок капитала.')
        print(out/'comparison.csv')

if __name__=='__main__':
    main()
