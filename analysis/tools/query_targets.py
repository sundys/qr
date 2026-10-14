#!/usr/bin/env python3
"""Query the parsed ObjC class dump for ad-hook targets."""
import json, re, sys

classes = json.load(open('objc_classes.json', encoding='utf-8'))
cats = json.load(open('objc_cats.json', encoding='utf-8'))
by_name = {c['name']: c for c in classes}

out = []
def w(s=''):
    out.append(s)

def show(c, header=None):
    w(header or f"=== {c['name']} : {c.get('super')} ===")
    for m in c['methods']:
        w('   - ' + m)

# 1. app's own Swift classes
w('########## APP OWN CLASSES (SimpleQR) ##########')
for c in sorted(classes, key=lambda x: x['name']):
    if 'SimpleQR' in c['name']:
        show(c)
        w()

# 2. AdMob (GAD) key entry points
w('########## ADMOB / GAD ENTRY POINTS ##########')
for nm in ['GADMobileAds', 'GADAppOpenAd', 'GADInterstitialAd', 'GADRewardedAd',
           'GADBannerView', 'GADRequest', 'GADAdLoader', 'GADAdReward',
           'GADAdValue', 'GADResponseInfo', 'GADMediatedUnifiedNativeAdNotificationSource',
           'GADMediationAdRequest', 'GADFullScreenContentDelegate', 'GADAdFormat',
           'GADMobileAdsConfiguration', 'GADAdapterStatus']:
    c = by_name.get(nm)
    if c:
        show(c)
        w()

# 3. Vungle
w('########## VUNGLE ##########')
for c in classes:
    if 'Vungle' in c['name'] and any(k in c['name'] for k in
        ('Banner','Interstitial','Rewarded','Ad','Ads','Placement','SDK','Native','Presenter','FullScreen','AppOpen')):
        show(c)

# 4. Pangle / PAG (CSJ)
w('########## PANGLE / PAG ##########')
for c in classes:
    if c['name'].startswith('PAG') or 'BUC' in c['name'] or 'CSJ' in c['name'] or 'Pangle' in c['name']:
        show(c)

# 5. Moloco / InMobi
w('########## MOLOCO ##########')
for c in classes:
    if 'Moloco' in c['name'] or c['name'].startswith('Moloco') or 'MOL' in c['name']:
        if any(k in c['name'] for k in ('Ad','Ads','Interstitial','Banner','Rewarded','SDK','Presenter','FullScreen')):
            show(c)
w('########## INMOBI ##########')
for c in classes:
    if c['name'].startswith('IM') and any(k in c['name'] for k in ('Ad','Interstitial','Banner','Rewarded','Native','SDK')):
        show(c)

# 6. purchase / remove ads
w('########## PURCHASE / IAP / REMOVE-ADS ##########')
pat = re.compile(r'StoreKit|SKPayment|SKProduct|SKMutablePayment|Purchas|Subscri|Paywall|RemoveAd|AdFree|Premium|Restore', re.I)
for c in classes:
    if pat.search(c['name']):
        show(c)

open('ad_hook_targets.txt', 'w', encoding='utf-8').write('\n'.join(out))
print(f'wrote ad_hook_targets.txt ({len(out)} lines)')
