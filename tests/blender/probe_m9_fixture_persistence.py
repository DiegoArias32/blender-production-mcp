"""Verify that the complete synthetic image library survives save/reopen.

Run in an owned --background --factory-startup --disable-autoexec process.
No renders, user projects or original fixture files are overwritten.
"""
from __future__ import annotations

from array import array
import hashlib
import json
from pathlib import Path
import sys
import traceback

sys.path.insert(0,str(Path(__file__).parent))
from fixtures_m9 import build


def pixel_record(image):
    values=array('f',image.pixels)
    return {'sha256':hashlib.sha256(values.tobytes()).hexdigest(),'count':len(values),
            'minimum':min(values),'maximum':max(values),'fake_user':image.use_fake_user,
            'colorspace':image.colorspace_settings.name}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(output):
    import bpy
    folder=Path(output).resolve();folder.mkdir(exist_ok=False,parents=True)
    report={'status':'RUNNING','checks':[],'errors':[]}
    def check(name,value):
        report['checks'].append({'name':name,'passed':bool(value)})
        if not value:raise AssertionError(name)
    try:
        fixture=build(folder/'fixtures')
        definitions=fixture['texture_definitions'];names=[d['image'] for d in definitions.values()]
        before={name:pixel_record(bpy.data.images[name]) for name in names}
        source=Path(fixture['source_file']);backup=Path(fixture['source_backup'])
        original=file_hash(source);backup_sha=file_hash(backup)
        bpy.ops.wm.open_mainfile(filepath=str(source),use_scripts=False)
        check('all seven declared images survive save and reopen',all(bpy.data.images.get(name) is not None for name in names))
        after={name:pixel_record(bpy.data.images[name]) for name in names}
        report.update(before=before,after=after,fixture=fixture)
        check('all declared library images retain fake user',all(i['fake_user'] for i in after.values()))
        check('all channel pixels and colorspaces remain byte identical',before==after)
        check('all seven PNG hashes match declared content',all(file_hash(d['path'])==d['sha256'] for d in definitions.values()))
        check('normal map remains variable and valid',definitions['NORMAL']['pixel_validation']['passed']
              and after[definitions['NORMAL']['image']]['maximum']>.9)
        check('all23 fixture objects and materials survive',len(fixture['cases'])==23
              and all(bpy.data.objects.get(r['object_name']) and bpy.data.materials.get(r['material_name']) for r in fixture['cases'].values()))
        check('original blend and backup unchanged by reopen',file_hash(source)==original and file_hash(backup)==backup_sha==original)
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append({'error':str(exc),'traceback':traceback.format_exc()})
    report['passed']=sum(c['passed'] for c in report['checks']);report['total']=len(report['checks'])
    with (folder/'report.json').open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
    print('__M9_FIXTURE_PERSISTENCE__='+json.dumps({'status':report['status'],'passed':report['passed'],
          'total':report['total'],'report':str(folder/'report.json')}))
    if report['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':main(sys.argv[sys.argv.index('--')+1])
