"""Execute notebook code cells sequentially in fresh IPython processes.

Uses the interpreter launching this script; no global Jupyter kernel registration.
Equivalent cell-order execution, not a test of the Jupyter frontend.
"""
from pathlib import Path
import sys, json, subprocess, contextlib, io, base64, hashlib

ROOT = Path(__file__).resolve().parents[1]

def execute(path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from IPython.core.interactiveshell import InteractiveShell
    from traitlets.config import Config
    config = Config()
    config.HistoryManager.enabled = False
    shell = InteractiveShell.instance(config=config)
    path = Path(path)
    notebook = json.loads(path.read_text(encoding='utf-8'))
    figure_count = 0
    outputs = []
    def show(*args, **kwargs):
        nonlocal figure_count
        for number in plt.get_fignums():
            figure_count += 1
            fig = plt.figure(number)
            dest = ROOT / 'figures' / f'{path.stem}_{figure_count:02d}.png'
            fig.savefig(dest, dpi=130, bbox_inches='tight')
            outputs.append({'output_type':'display_data','metadata':{},'data':{
                'image/png':base64.b64encode(dest.read_bytes()).decode('ascii'),
                'text/plain':[f'Figure {figure_count}']}})
        plt.close('all')
    plt.show = show
    count = 0
    for index, cell in enumerate(notebook['cells']):
        if cell['cell_type'] != 'code': continue
        source = ''.join(cell['source'])
        if not source.strip(): continue
        count += 1
        outputs = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = shell.run_cell(source, store_history=False)
        for name, stream in [('stdout',out),('stderr',err)]:
            if stream.getvalue():
                text = stream.getvalue().replace(str(ROOT), '<PROJECT_ROOT>')
                outputs.append({'output_type':'stream','name':name,'text':text.splitlines(True)})
        cell['execution_count'] = count
        cell['outputs'] = outputs
        if result.error_before_exec or result.error_in_exec:
            print(out.getvalue(), err.getvalue())
            raise RuntimeError(f'{path.name}: cell {index} failed')
    path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1),encoding='utf-8')
    print(f'PASS {path.name}: {count} code cells; {figure_count} figures')

if __name__ == '__main__':
    if len(sys.argv)>1:
        execute(sys.argv[1])
    else:
        raw = list((ROOT/'data/raw').glob('*'))
        before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw}
        for path in sorted((ROOT/'notebooks').glob('*.ipynb')):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), str(path)],cwd=ROOT,check=True)
        after = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw}
        assert before == after, 'Frozen raw data changed'
        print('PASS: frozen raw data hashes unchanged')
