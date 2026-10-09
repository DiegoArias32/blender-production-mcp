"""Host adapter for owned background fixtures; no socket, addon or GUI.

Blender executes only commands composed by the existing build_code/safe_mode
validator. This test transport is not a replacement production MCP server.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from blender_mcp.production.common.bridge import MARKER, build_code

REPO = Path(__file__).resolve().parents[2]
DEFAULT_BLENDER = 'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'


class PipeBridge:
    def __init__(self, folder, fps=30, duration=2, variant='cloth_heavy', blender=DEFAULT_BLENDER):
        folder = Path(folder).resolve()
        folder.mkdir(parents=True,exist_ok=True)
        self.log = (folder/'blender-stdout.txt').open('x',encoding='utf-8')
        env = dict(os.environ,BLENDER_MCP_SAFE_MODE='1',DISABLE_TELEMETRY='true')
        self.process = subprocess.Popen([str(blender),'--background','--factory-startup','--disable-autoexec',
            '--python',str(Path(__file__).with_name('m8_pipe_worker.py')),'--',str(folder),str(fps),str(duration),variant],
            cwd=REPO,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            encoding='utf-8',errors='replace',bufsize=1)
        try:
            self.fixture = self.read('__M8_PIPE_READY__=')
        except Exception:
            self.close()
            raise

    def read(self,prefix):
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError('OWNED_BLENDER_PROCESS_ENDED: '+str(self.process.poll()))
            self.log.write(line)
            self.log.flush()
            if line.startswith(prefix):
                return json.loads(line[len(prefix):])

    def run(self,body,params=None):
        code = build_code(body,params)
        self.process.stdin.write(json.dumps({'code':code},allow_nan=False)+'\n')
        self.process.stdin.flush()
        reply = self.read('__M8_PIPE_REPLY__=')
        if reply.get('error'):
            raise RuntimeError(reply['error']+'\n'+reply.get('traceback',''))
        rows = [line[len(MARKER):] for line in reply['output'].splitlines() if line.startswith(MARKER)]
        if len(rows)!=1:
            raise RuntimeError('LOCAL_BRIDGE_RESULT_NOT_UNIQUE')
        return json.loads(rows[0])

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write(json.dumps({'quit':True})+'\n')
                self.process.stdin.flush()
                self.process.wait(timeout=30)
            except (OSError,subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=15)
        self.log.close()
