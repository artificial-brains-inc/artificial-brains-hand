import sys,json,time,http.client
from urllib.parse import urlsplit
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUT=ROOT/'test-results'
OUT.mkdir(exist_ok=True)
from aerohand_control.ui import LocalUI
from aerohand_control.app import pose_menu
from aerohand_control.constants import FINGERS
from playwright.sync_api import sync_playwright
received=[]
ui=LocalUI(pose_menu(),received.append)
state=dict(mode='live',version='0.0.0',status='hold',command='hold',device='SOFTWARE TEST: no hand',step=10,
           position=[20.]*7,goal=[40.]*7,limits=[[0,100]]*7,amount=1.,message='Synthetic UI test fixture. No physical devices.',
           tactile={'fresh':True,'adc':dict(zip(FINGERS,[200,300,400,500,600])),
                    'normalized':dict(zip(FINGERS,[v/4095 for v in (200,300,400,500,600)])),'holds':{'thumb':0,'index':1,'middle':0,'ring':0,'little':0},
                    'baseline':dict(zip(FINGERS,[80,90,100,2000,110])),'noise_deadband':dict.fromkeys(FINGERS,0),'directions':{f:1 for f in FINGERS},'port':'fixture','age_s':.01})
state['position'][0]=17.1;state['goal'][0]=17.
state['thumb_setup']={'locked':True,'target':17.,'measured':17.1,'saved':None,'limits':[0,100],'can_adjust':True}
ui.update(state); url=ui.start(open_browser=False)
checks=[];errors=[]
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(**({'executable_path': __import__('os').environ['CHROMIUM_EXECUTABLE']} if __import__('os').environ.get('CHROMIUM_EXECUTABLE') else {}), headless=True, args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1440,'height':1100})
  page.on('pageerror',lambda e:errors.append(str(e)))
  parts=urlsplit(url)
  # Offline browser DOM test: a binding transports fetch calls to the real local server without
  # relying on browser network policy.
  def bridge(source,path,options):
    con=http.client.HTTPConnection(parts.hostname,parts.port,timeout=5)
    method=options.get('method','GET'); headers={'Host':parts.netloc}
    if method=='POST': headers.update({'Origin':'http://'+parts.netloc,'Content-Type':'application/json'})
    con.request(method,parts.path+path,body=options.get('body'),headers=headers)
    response=con.getresponse();body=response.read().decode();con.close()
    return {'status':response.status,'body':body}
  page.expose_binding('testTransport',bridge)
  root=ROOT/'aerohand_control/web'
  html=(root/'index.html').read_text().replace('<script src="app.js" defer></script>','').replace('<link rel="stylesheet" href="style.css">','').replace('<link rel="stylesheet" href="brand.css">','')
  import base64
  logo=root/'assets/artificial-brains-logo.png'
  # Render the same bundled bitmap while using the offline HTTP bridge.
  html=html.replace('assets/artificial-brains-logo.png','data:image/png;base64,'+base64.b64encode(logo.read_bytes()).decode())
  page.set_content(html)
  page.add_style_tag(content=(root/'style.css').read_text())
  page.add_style_tag(content=(root/'brand.css').read_text())
  page.evaluate("() => { window.fetch = async (path,options={}) => { const r=await window.testTransport(path,{method:options.method||'GET',body:options.body||null}); return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}}); }; }")
  page.add_script_tag(content=(root/'app.js').read_text())
  page.wait_for_function("document.querySelectorAll('.tactile-card').length===5 && document.querySelector('[data-command=grab]')?.disabled===false")
  assert page.locator('.pose-button').count()==9;checks.append('nine pose controls and five tactile cards render')
  assert page.title().endswith(' · Artificial Brains')
  assert page.locator('.ab-logo').evaluate('(img)=>img.complete && img.naturalWidth>0')
  assert page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(8, 11, 16)'
  checks.append('real Artificial Brains logo loads; charcoal/lavender dark theme')
  con=http.client.HTTPConnection(parts.hostname,parts.port,timeout=5)
  con.request('GET',parts.path+'assets/artificial-brains-logo.png',headers={'Host':parts.netloc})
  response=con.getresponse();data=response.read();con.close()
  assert response.status==200 and data==logo.read_bytes()
  checks.append('bundled logo served locally without external dependency')
  state.update(status='opening',command='open',startup={'state':'opening','samples':0,'confirmed':False})
  ui.update(state);page.wait_for_timeout(400)
  assert page.locator('[data-command="grab"]').is_disabled()
  assert not page.locator('#hold-button').is_disabled() and not page.locator('#stop-button').is_disabled()
  assert page.locator('#current-command').inner_text()=='Open hand'
  assert 'BRAIN START' in page.locator('#startup-note').inner_text()
  page.screenshot(path=str(OUT/'ui-opening.png'),full_page=True)
  checks.append('opening phase shows brain not started and leaves Hold/Stop usable')
  state.update(status='zeroing',command='hold',startup={'state':'zeroing','samples':24,'confirmed':True})
  ui.update(state);page.wait_for_timeout(350)
  assert page.locator('[data-command="grab"]').is_disabled()
  assert '24 / 50' in page.locator('#startup-note').inner_text()
  checks.append('startup unloaded zeroing visible; pose dispatch disabled until ready')
  state.update(status='hold',command='hold',startup={'state':'ready','confirmed':True})
  ui.update(state);page.wait_for_timeout(350)

  assert page.locator('#thumb-angle').input_value()=='17.0'
  assert page.locator('#target-inputs input').nth(0).is_disabled()
  checks.append('manual angle displayed; ordinary coordinate field cannot edit abduction')
  before=len(received)
  page.locator('#thumb-slider').fill('32')
  page.locator('#thumb-slider').dispatch_event('input');page.wait_for_timeout(100)
  assert len(received)==before
  checks.append('slider editing alone sends no hardware request')
  page.locator('#thumb-apply').click();page.wait_for_timeout(150)
  assert received[-1]=='thumb_angle 32'
  state['thumb_setup'].update(target=32.,measured=31.8,saved=32.)
  ui.update(state);page.wait_for_timeout(300)
  assert '32.0°' in page.locator('#thumb-readout').inner_text()
  checks.append('Apply and save queues manual angle and displays target versus measured')
  page.locator('#thumb-plus').click();page.wait_for_timeout(150)
  assert received[-1]=='thumb_angle 33'
  state['thumb_setup'].update(target=33.,saved=33.)
  ui.update(state);page.wait_for_timeout(300)
  page.locator('#thumb-minus').click();page.wait_for_timeout(150)
  assert received[-1]=='thumb_angle 32'
  checks.append('plus and minus buttons queue one-degree manual adjustments')
  state['thumb_setup'].update(target=32.,saved=32.)
  ui.update(state);page.wait_for_timeout(300)
  page.locator('#thumb-current').click();page.wait_for_timeout(150)
  assert received[-1]=='thumb_current'
  checks.append('Lock measured position is a separate operator request')
  state['thumb_setup'].update(target=17.,measured=17.1,saved=17.)
  ui.update(state);page.wait_for_timeout(300)
  state.update(mode='observe')
  ui.update(state);page.wait_for_timeout(300)
  assert page.locator('#thumb-apply').is_disabled() and page.locator('#thumb-current').is_disabled()
  checks.append('manual movement disabled in observe-only mode')
  state.update(mode='live')
  ui.update(state);page.wait_for_timeout(300)
  assert page.locator('#capture-zero').count()==1
  page.locator('#capture-zero').click();page.wait_for_timeout(150)
  assert received[-1]=='calibrate_zero'
  assert 'Capture enough contact' not in page.locator('body').inner_text()
  checks.append('unloaded zero button submits a generation-checked request; no grip calibration')
  page.locator('[data-command="grab"]').click();page.wait_for_timeout(150)
  assert received[-1]=='grab 1';checks.append('grab available without calibration metadata or file')
  assert page.locator('.tactile-card').nth(1).locator('.neural-hold').inner_text()=='Neural HOLD: ACTIVE';checks.append('neural hold displayed separately from ADC')
  page.locator('#use-measured').click();assert page.locator('#target-inputs input').nth(0).input_value()=='17.00'
  page.locator('#send-targets').click();page.wait_for_timeout(150);assert received[-1].startswith('targets 17');checks.append('seven coordinate values submitted')
  page.locator('[data-command="grab"]').click();page.wait_for_timeout(150);assert received[-1]=='grab 1';checks.append('pose still sends coordinate-preset request')
  state.update(status='active',command='grab');ui.update(state);page.wait_for_timeout(400)
  assert page.locator('#capture-zero').is_disabled();checks.append('zeroing unavailable while moving')
  assert page.locator('#thumb-apply').is_disabled() and page.locator('#thumb-plus').is_disabled();checks.append('manual thumb changes require Hold, not an active grasp')
  page.locator('[data-command="grab"]').focus();page.keyboard.press('Space');page.wait_for_timeout(150);assert received[-1]=='hold';checks.append('Space requests Hold')
  state.update(status='hold',command='hold');ui.update(state);page.wait_for_timeout(350)
  state['tactile']['normalized']['index']=1.;state['tactile']['adc']['index']=4095;state['tactile']['holds']['index']=0
  ui.update(state);page.wait_for_timeout(350)
  assert page.locator('.tactile-card').nth(1).locator('.neural-hold').inner_text()=='Neural HOLD: off'
  checks.append('full ADC alone does not create a displayed HOLD')
  state['tactile']['normalized']['index']=0.;state['tactile']['adc']['index']=0;state['tactile']['holds']['index']=1
  ui.update(state);page.wait_for_timeout(350)
  assert page.locator('.tactile-card').nth(1).locator('.neural-hold').inner_text()=='Neural HOLD: ACTIVE'
  checks.append('neural HOLD displays even at zero ADC; no browser threshold')
  # Synthetic unloaded inputs for a UI test, not measurements from a physical hand.
  state['tactile']['adc']=dict(zip(FINGERS,[80,90,100,2000,110]))
  state['tactile']['normalized']=dict.fromkeys(FINGERS,0.0)
  state['tactile']['holds']=dict.fromkeys(FINGERS,0)
  ui.update(state);page.wait_for_timeout(350)
  assert '2000' in page.locator('.tactile-card').nth(3).inner_text() and '0.000' in page.locator('.tactile-card').nth(3).inner_text()
  checks.append('synthetic unloaded baseline displayed with zero corrected input')
  page.screenshot(path=str(OUT/'ui-desktop.png'),full_page=True)
  state['tactile']['fresh']=False;ui.update(state);page.wait_for_timeout(400)
  assert page.locator('[data-command="grab"]').is_disabled();assert not page.locator('#hold-button').is_disabled();assert not page.locator('#stop-button').is_disabled();checks.append('stale sensor view keeps Hold/Stop usable')
  state['tactile']['fresh']=True;ui.update(state)
  page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(400)
  assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1');checks.append('mobile width has no horizontal overflow')
  page.screenshot(path=str(OUT/'ui-mobile.png'),full_page=True)
  assert not errors,errors
  checks.append('no browser JavaScript errors')
  browser.close()
finally: ui.close()
(OUT/'browser_report.json').write_text(json.dumps({'checks':checks,'errors':errors,'hardware_contacted':False,'mode':'offline DOM with real-local-HTTP bridge; direct browser navigation blocked by environment'},indent=2))
print(json.dumps({'passed':len(checks),'checks':checks},indent=2))
