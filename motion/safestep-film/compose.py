"""Original 60-second SafeStep score: stereo felt keys, strings, pulses and air.
Deterministic synthesis; no samples or externally licensed music.
"""
import numpy as np, wave, json
from pathlib import Path
ROOT=Path(__file__).parent
SR=48000; DUR=60; rng=np.random.default_rng(181)
a=np.zeros((SR*DUR,2),np.float64)
def hz(n): return 440*2**((n-69)/12)
def add(x,start,level=1,pan=0):
    i=int(start*SR)
    if i<0: x=x[-i:]; i=0
    n=min(len(x),len(a)-i)
    if n<=0:return
    pan=np.clip(pan,-1,1)
    if x.ndim==1: x=x[:,None]*np.array([np.sqrt((1-pan)/2),np.sqrt((1+pan)/2)])
    a[i:i+n]+=x[:n]*level

def pad(notes,start,dur,level=.15):
    t=np.arange(int((dur+2)*SR))/SR
    env=np.minimum(t/1.8,1)*np.minimum(np.maximum(dur+2-t,0)/2.2,1)
    snd=np.zeros((len(t),2))
    for k,n in enumerate(notes):
        f=hz(n)
        for ch,det in enumerate([-.0018,.0018]):
            snd[:,ch]+=(np.sin(2*np.pi*f*(1+det)*t+.2*k)+.19*np.sin(2*np.pi*f*2.001*t)+.055*np.sin(2*np.pi*f*3*t))*env*(1+.04*np.sin(t*2.1+k))/len(notes)
    add(snd,start,level)

def key(n,start,level=.08,pan=0):
    t=np.arange(int(3.8*SR))/SR;f=hz(n)
    env=(1-np.exp(-t*90))*np.exp(-t*1.5)
    x=(np.sin(2*np.pi*f*t)+.32*np.sin(2*np.pi*f*2.003*t)*np.exp(-t*1.2)+.12*np.sin(2*np.pi*f*3.99*t)*np.exp(-t*2.7))*env
    add(x,start,level,pan)
    add(x,start+.357,level*.22,-pan);add(x,start+.714,level*.11,pan*.6)

def bass(n,start,dur,level=.075):
    t=np.arange(int(dur*SR))/SR;env=np.minimum(t/.05,1)*np.minimum((dur-t)/.4,1)
    add((np.sin(2*np.pi*hz(n)*t)+.12*np.sin(2*np.pi*hz(n)*2*t))*env,start,level)

def sweep(start,dur=1.2,level=.035):
    n=int(dur*SR);t=np.arange(n)/SR;noise=rng.standard_normal(n)
    low=np.convolve(noise,np.ones(37)/37,mode='same');env=np.sin(np.pi*t/dur)**2
    add(low*env,start,level,-.2)

def pulse(start,level=.065):
    t=np.arange(int(.46*SR))/SR
    x=np.sin(2*np.pi*(48*t+2*(1-np.exp(-t*18))))*np.exp(-t*13)*(1-np.exp(-t*200))
    add(x,start,level)

# Suspense to warm open harmonies. Musical boundaries follow the film's acts.
pad([38,50,57,64],0,6.4,.15)
key(74,.2,.075,-.45);key(69,2.55,.055,.45);key(76,4.3,.04,-.2)
chords=[[50,57,60,64,69],[46,53,57,60,65],[41,53,57,60,67],[48,55,60,62,67]]
step=60/84; bar=step*4
for k in range(17):
    start=6.417+k*bar
    if start>=54.58:break
    chord=chords[k%4];pad(chord,start,bar,.125)
    bass(chord[0]-12,start,bar,.055)
    if start>12:
        seq=[chord[1]+12,chord[2]+12,chord[3]+12,chord[4]+12,chord[3]+12,chord[2]+12,chord[1]+12,chord[3]+12]
        for j,n in enumerate(seq):key(n,start+j*step/2,.027 if start<33 else .04,np.sin(j*.9)*.65)
    for j in range(4):
        if start+j*step<54:pulse(start+j*step,.032 if start<26 else .045)
    if start>26:
        for j in range(4):
            tm=start+(j+.5)*step
            n=int(.09*SR);z=rng.standard_normal(n);z=np.concatenate([[0],np.diff(z)])*np.exp(-np.arange(n)/SR*65)
            add(z,tm,.005,(-1)**j*.35)
for sc in json.load(open(ROOT/'build/timeline.json'))['scenes'][1:]:
    sweep(sc['start']-.65,1.1,.09);key(81,sc['start']+.05,.045,.2)
# Lift into the brand: resolve and leave room for the final line.
pad([41,53,57,60,67,72],54.58,5.5,.2)
for i,n in enumerate([65,69,72,79]):key(n,54.7+i*.14,.07,(i-1.5)*.3)
bass(29,54.58,5.4,.075)
# A subtle stereo diffuse tail, calculated from the dry score.
dry=a.copy()
for delay,gain in [(.061,.11),(.101,.09),(.173,.07),(.283,.06),(.431,.05),(.683,.035)]:
    d=int(delay*SR);a[d:]+=dry[:-d,::-1]*gain
# Symmetric headroom, gentle saturation and a deliberate two-second tail.
a=np.tanh(a*1.15)
a*=np.minimum(np.arange(len(a))/SR/.3,1)[:,None]
a*=np.minimum((DUR-np.arange(len(a))/SR)/2,1)[:,None]
a*=.86/max(np.max(np.abs(a)),.001)
with wave.open(str(ROOT/'assets/every-step-score.wav'),'wb') as w:
    w.setnchannels(2);w.setsampwidth(2);w.setframerate(SR);w.writeframes((a*32767).astype('<i2').tobytes())
print('Original stereo score: 60.0 seconds, 48 kHz')
