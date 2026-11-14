"""Open Graph image for social sharing (1200x630 PNG).

The official NEXORA logo is composited directly from the brand asset. The image
is promotional and deliberately shows no fictional balances, P&L or earnings.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).parent
W,H=1200,630

def font(size,bold=False):
    candidates=[
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    ]
    for name in candidates:
        if Path(name).is_file():
            return ImageFont.truetype(name,size)
    return ImageFont.load_default()

def roundrect(d,coords,r,fill,outline=None,width=1):
    d.rounded_rectangle(coords,radius=r,fill=fill,outline=outline,width=width)

@lru_cache(maxsize=1)
def create_social_preview():
    bg=Image.new('RGB',(W,H),(5,18,32))
    pix=bg.load()
    # Restrained blue gradient, with cyan-to-gold brand accents.
    for y in range(H):
        for x in range(W):
            bloom=max(0,1-((x-1020)**2/(1000**2)+(y-120)**2/(510**2))**.5)
            edge=max(0,1-abs(y-500)/310)
            pix[x,y]=(int(5+17*bloom+2*edge),int(18+34*bloom+8*edge),int(32+43*bloom+15*edge))
    d=ImageDraw.Draw(bg)
    d.arc((-240,270,1500,760),3,175,fill=(32,130,173),width=3)
    d.arc((-320,340,1440,800),5,180,fill=(159,123,58),width=3)

    # Official brand artwork, unaltered apart from proportional resizing.
    logo=ROOT/'static'/'logo-nexora-web.png'
    if not logo.is_file():logo=ROOT/'static'/'logo-top.png'
    if logo.is_file():
        try:
            with Image.open(logo) as source:
                im=source.convert('RGBA')
                im.thumbnail((380,115),Image.Resampling.LANCZOS)
                bg.paste(im,(68,42),im)
        except OSError:
            pass

    gold=(248,192,80);white=(243,248,253);soft=(191,209,223)
    d.text((70,195),'O ROBÔ TRADER',font=font(58,True),fill=white)
    d.text((70,267),'AUTOMÁTICO',font=font(62,True),fill=gold)
    d.text((70,356),'MAIS PRÁTICO PARA VOCÊ',font=font(30,True),fill=white)
    d.text((70,416),'Teste grátis por 24 horas',font=font(26),fill=soft)
    roundrect(d,(70,490,410,561),19,(249,195,86))
    d.text((103,508),'TESTAR GRÁTIS 24H',font=font(23,True),fill=(16,31,47))

    # Product-style illustration: no invented profit/balances.
    roundrect(d,(748,134,1147,525),22,(8,28,46),outline=(214,172,92),width=3)
    d.text((780,167),'NEXORA IA',font=font(25,True),fill=white)
    d.text((780,202),'Meu robô',font=font(33,True),fill=white)
    roundrect(d,(776,259,1117,396),16,(18,49,73),outline=(47,99,128),width=2)
    d.text((798,282),'ROBÔ AUTOMÁTICO',font=font(17,True),fill=(214,228,238))
    roundrect(d,(798,321,957,373),12,(248,191,72))
    d.text((817,337),'LIGAR ROBÔ',font=font(17,True),fill=(18,31,44))
    roundrect(d,(972,321,1097,373),11,(20,60,76),outline=(50,130,143))
    d.text((990,337),'PAINEL',font=font(17,True),fill=white)
    d.text((776,424),'Análise de mercado',font=font(17),fill=soft)
    d.text((776,458),'Gestão de risco  ·  AutoPilot',font=font(15),fill=soft)
    d.text((70,588),'NEXORA IA  •  ROBÔ TRADER AUTOMÁTICO',font=font(14,True),fill=(135,161,182))
    out=BytesIO()
    bg.save(out,format='PNG',optimize=True)
    return out.getvalue()
