// AdsKiller.m — SimpleQR (qrcode 4.0.3) 去广告注入 dylib
//
// 纯 Objective-C runtime swizzling，不依赖 CydiaSubstrate / Theos 运行时，
// 因此可以对「未越狱 + 重签名侧载」的 IPA 使用（TrollStore / Sideloadly / AltStore）。
//
// 所有 selector 均从 SimpleQR(4.0.3) 主二进制的 __objc_classlist / metaclass 中实测提取，
// 与之完全一致（见 analysis/ad_hook_targets.txt）。
//
// 设计原则：只拦截「加载」和「展示」两端，不改 SDK 初始化，
// 这样 App 自己的失败回调（bannerView:didFailToReceiveAdWithError: 等）会正常触发，
// 横幅高度由 App 自己收起，不需要我们去改布局。

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <objc/runtime.h>
#import <dispatch/dispatch.h>

// 用协议声明代替 objc_msgSend 强转：ARC 下 objc_msgSend 强转容易编译报错，
// 这里只是给编译器一个契约，运行时用的还是 GADMobileAds 自己的实现。
@protocol AKGADMobileAdsShim <NSObject>
+ (instancetype)sharedInstance;
- (void)disableMediationInitialization;
@end

// 同理：给 banner 的 delegate 一个契约，否则 ARC 下对 id 发未声明的 selector 会直接编译报错
// （GADBannerViewDelegate 的真实签名与此一致）
@protocol AKBannerDelegateShim <NSObject>
- (void)bannerView:(id)bannerView didFailToReceiveAdWithError:(NSError *)error;
@end

#define AK_LOG(fmt, ...) NSLog(@"[AdsKiller] " fmt, ##__VA_ARGS__)

// ---------- 可配置开关 ----------
// 1 = 在启动时调用 AdMob 官方 API 关闭聚合中介初始化（Pangle/Vungle/Moloco/InMobi 适配器不再初始化）
#define AK_DISABLE_MEDIATION_INIT 1
// 1 = 加载被拦截时，主动回调 delegate 的失败方法，让宿主 App 走自己的“广告加载失败→收起横幅”逻辑
#define AK_NOTIFY_BANNER_FAILURE 1
// 1 = 连 AdMob SDK 初始化也拦掉（更彻底，但个别 App 拿到 nil 状态会崩，默认关闭）
#define AK_KILL_SDK_INIT 0
// 1 = 把横幅视图的 frame 高度置 0（宿主 App 不收起横幅时的兜底）
#define AK_COLLAPSE_BANNER_VIEW 0

// ---------- runtime 工具 ----------

static void AKSwizzleClassMethod(Class cls, SEL sel, IMP newImp, IMP *outOld) {
    if (!cls || !sel) return;
    Method m = class_getClassMethod(cls, sel);
    if (!m) { AK_LOG(@"class method not found: +[%@ %@]", cls, NSStringFromSelector(sel)); return; }
    if (outOld) *outOld = method_setImplementation(m, newImp);
    AK_LOG(@"hooked +[%@ %@]", cls, NSStringFromSelector(sel));
}

static void AKSwizzleInstanceMethod(Class cls, SEL sel, IMP newImp, IMP *outOld) {
    if (!cls || !sel) return;
    Method m = class_getInstanceMethod(cls, sel);
    if (!m) { AK_LOG(@"instance method not found: -[%@ %@]", cls, NSStringFromSelector(sel)); return; }
    if (outOld) *outOld = method_setImplementation(m, newImp);
    AK_LOG(@"hooked -[%@ %@]", cls, NSStringFromSelector(sel));
}

static Class AKClass(NSString *name) {
    Class c = NSClassFromString(name);
    if (!c) AK_LOG(@"class not present (skip): %@", name);
    return c;
}

static NSError *AKFakeError(void) {
    return [NSError errorWithDomain:@"com.google.ads" code:0 userInfo:nil];
}

// 让宿主 App 走“加载失败”分支（它自己的 delegate 会收起横幅高度）
static void AKNotifyBannerFailure(id bannerView) {
#if AK_NOTIFY_BANNER_FAILURE
    @try {
        id<AKBannerDelegateShim> delegate =
            (id<AKBannerDelegateShim>)[bannerView valueForKey:@"delegate"];
        SEL sel = @selector(bannerView:didFailToReceiveAdWithError:);
        if (delegate && [delegate respondsToSelector:sel]) {
            [delegate bannerView:bannerView didFailToReceiveAdWithError:AKFakeError()];
        }
    } @catch (NSException *e) { AK_LOG(@"notify banner failure: %@", e); }
#endif
#if AK_COLLAPSE_BANNER_VIEW
    @try {
        if ([bannerView isKindOfClass:[UIView class]]) {
            UIView *v = (UIView *)bannerView;
            CGRect f = v.frame; f.size.height = 0; v.frame = f;
        }
    } @catch (NSException *e) {}
#endif
}

// ======================= AdMob (GAD) =======================

// +[GADAppOpenAd|GADInterstitialAd|GADRewardedAd loadWithAdUnitID:request:completionHandler:]
static void AKLoadFullScreenAd(id self, SEL _cmd, id adUnitID, id request, void (^completion)(id, NSError *)) {
    AK_LOG(@"blocked %@ %@", NSStringFromClass([self class]), NSStringFromSelector(_cmd));
    if (completion) completion(nil, AKFakeError());
}

// +[GADInterstitialAd loadFullScreenAdWithAdUnitID:request:completionHandler:]
static void AKLoadInterstitialAd(id self, SEL _cmd, id adUnitID, id request, void (^completion)(id, NSError *)) {
    AK_LOG(@"blocked +[GADInterstitialAd loadFullScreenAdWithAdUnitID:...]");
    if (completion) completion(nil, AKFakeError());
}

// -[GADAppOpenAd|GADInterstitialAd presentFromRootViewController:]
static void AKPresent(id self, SEL _cmd, UIViewController *vc) {
    AK_LOG(@"blocked present: %@", NSStringFromClass([self class]));
}

// -[GADRewardedAd presentFromRootViewController:userDidEarnRewardHandler:]
static void AKPresentRewarded(id self, SEL _cmd, UIViewController *vc, id handler) {
    AK_LOG(@"blocked rewarded present: %@", NSStringFromClass([self class]));
}

// -[GADBannerView loadRequest:] / -[GADAdLoader loadRequest:]
static void AKLoadRequest(id self, SEL _cmd, id request) {
    AK_LOG(@"blocked loadRequest: on %@", NSStringFromClass([self class]));
    if ([self isKindOfClass:[UIView class]]) AKNotifyBannerFailure(self);
}

// -[GADAdLoader loadRequestWithTarget:] / -[GADBannerView loadWithTargeting:] 等内部入口
static void AKLoadRequestTarget(id self, SEL _cmd, id target) {
    AK_LOG(@"blocked %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
}

// -[GADBannerView loadWithAdResponseString:] / -[GADAdLoader loadWithAdResponseString:]
static void AKLoadResponseString(id self, SEL _cmd, id s) {
    AK_LOG(@"blocked %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
}

// +[GADAppOpenAd|GADInterstitialAd|GADRewardedAd preloadedAdWithAdUnitID:] —— 永远没有预加载广告
static id AKPreloadedAd(id self, SEL _cmd, id adUnitID) {
    return nil;
}

// +[GADMobileAds startWithCompletionHandler:] —— 可选
static void AKMobileAdsStart(id self, SEL _cmd, void (^completion)(id)) {
    AK_LOG(@"blocked -[GADMobileAds startWithCompletionHandler:]");
    if (completion) completion(nil);
}

// ======================= Vungle (Swift 类，ObjC 暴露) =======================

static void AKVunglePresent(id self, SEL _cmd, id arg) {
    AK_LOG(@"blocked Vungle %@ %@", NSStringFromClass([self class]), NSStringFromSelector(_cmd));
}

// ======================= Pangle / PAG =======================

// Pangle 的 AdMob 中介适配器（这两个类是 Pangle 接入 AdMob mediation 的桥）
static void AKPangleAdapterPresent(id self, SEL _cmd, UIViewController *vc) {
    AK_LOG(@"blocked Pangle mediation present");
}

// PAGLInterstitialAd presentFromRootViewController: / PAGNativeExpressRewardedVideoAd showAdFromRootViewController:
static void AKPangleShow(id self, SEL _cmd, UIViewController *vc) {
    AK_LOG(@"blocked Pangle show: %@", NSStringFromClass([self class]));
}

static void AKPangleShowRit(id self, SEL _cmd, UIViewController *vc, id a, id b) {
    AK_LOG(@"blocked Pangle show(rit): %@", NSStringFromClass([self class]));
}

// Pangle 原生/信息流：不是 present，而是把视图注册进容器（实测 PAGLNativeAd 没有 present 方法）
static void AKPangleRegisterContainer(id self, SEL _cmd, id container, id clickableViews) {
    AK_LOG(@"blocked Pangle native registerContainer:withClickableViews:");
}

// PAGNativeAdsManager loadAdDataWithCount:
static void AKPangleLoadNative(id self, SEL _cmd, id count) {
    AK_LOG(@"blocked Pangle native load: loadAdDataWithCount:");
}

// ======================= 装配 =======================

__attribute__((constructor))
static void AdsKillerInit(void) {
    @autoreleasepool {
        AK_LOG(@"=== AdsKiller loading ===");

        // ---- AdMob：全屏广告加载入口 ----
        Class appOpen = AKClass(@"GADAppOpenAd");
        Class interstitial = AKClass(@"GADInterstitialAd");
        Class rewarded = AKClass(@"GADRewardedAd");

        SEL loadSel = NSSelectorFromString(@"loadWithAdUnitID:request:completionHandler:");
        if (appOpen)     AKSwizzleClassMethod(appOpen, loadSel, (IMP)AKLoadFullScreenAd, NULL);
        if (interstitial) AKSwizzleClassMethod(interstitial, loadSel, (IMP)AKLoadFullScreenAd, NULL);
        if (rewarded)    AKSwizzleClassMethod(rewarded, loadSel, (IMP)AKLoadFullScreenAd, NULL);
        if (interstitial)
            AKSwizzleClassMethod(interstitial,
                NSSelectorFromString(@"loadFullScreenAdWithAdUnitID:request:completionHandler:"),
                (IMP)AKLoadInterstitialAd, NULL);

        // 预加载查询（防止中介层直接吐出已缓存广告）
        SEL preloadedSel = NSSelectorFromString(@"preloadedAdWithAdUnitID:");
        if (appOpen)      AKSwizzleClassMethod(appOpen, preloadedSel, (IMP)AKPreloadedAd, NULL);
        if (interstitial) AKSwizzleClassMethod(interstitial, preloadedSel, (IMP)AKPreloadedAd, NULL);
        if (rewarded)     AKSwizzleClassMethod(rewarded, preloadedSel, (IMP)AKPreloadedAd, NULL);

        // ---- AdMob：展示入口（兜底）----
        SEL presentSel = NSSelectorFromString(@"presentFromRootViewController:");
        if (appOpen)      AKSwizzleInstanceMethod(appOpen, presentSel, (IMP)AKPresent, NULL);
        if (interstitial) AKSwizzleInstanceMethod(interstitial, presentSel, (IMP)AKPresent, NULL);
        if (rewarded)
            AKSwizzleInstanceMethod(rewarded,
                NSSelectorFromString(@"presentFromRootViewController:userDidEarnRewardHandler:"),
                (IMP)AKPresentRewarded, NULL);

        // ---- AdMob：横幅 / 原生 ----
        Class banner = AKClass(@"GADBannerView");
        Class adLoader = AKClass(@"GADAdLoader");
        SEL loadReq = NSSelectorFromString(@"loadRequest:");
        if (banner)   AKSwizzleInstanceMethod(banner, loadReq, (IMP)AKLoadRequest, NULL);
        if (adLoader) AKSwizzleInstanceMethod(adLoader, loadReq, (IMP)AKLoadRequest, NULL);
        if (banner) {
            AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"loadWithTargeting:"), (IMP)AKLoadRequestTarget, NULL);
            AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"loadWithAdResponseString:"), (IMP)AKLoadResponseString, NULL);
        }
        if (adLoader) {
            AKSwizzleInstanceMethod(adLoader, NSSelectorFromString(@"loadRequestWithTarget:"), (IMP)AKLoadRequestTarget, NULL);
            AKSwizzleInstanceMethod(adLoader, NSSelectorFromString(@"loadWithAdResponseString:"), (IMP)AKLoadResponseString, NULL);
        }

#if AK_KILL_SDK_INIT
        Class mobileAds = AKClass(@"GADMobileAds");
        if (mobileAds)
            AKSwizzleInstanceMethod(mobileAds, NSSelectorFromString(@"startWithCompletionHandler:"), (IMP)AKMobileAdsStart, NULL);
#endif

        // ---- Vungle ----
        for (NSString *n in @[@"_TtC12VungleAdsSDK18VungleInterstitial",
                              @"_TtC12VungleAdsSDK14VungleRewarded",
                              @"_TtC12VungleAdsSDK12VungleBanner",
                              @"_TtC12VungleAdsSDK12BasePublicAd"]) {
            Class c = AKClass(n);
            if (!c) continue;
            if (class_getInstanceMethod(c, NSSelectorFromString(@"presentWith:")))
                AKSwizzleInstanceMethod(c, NSSelectorFromString(@"presentWith:"), (IMP)AKVunglePresent, NULL);
            if (class_getInstanceMethod(c, NSSelectorFromString(@"presentOn:")))
                AKSwizzleInstanceMethod(c, NSSelectorFromString(@"presentOn:"), (IMP)AKVunglePresent, NULL);
            if (class_getInstanceMethod(c, NSSelectorFromString(@"load:")))
                AKSwizzleInstanceMethod(c, NSSelectorFromString(@"load:"), (IMP)AKVunglePresent, NULL);
        }

        // ---- Pangle ----
        Class pagAdapter = AKClass(@"PAGInterfacePangleInterstitialAdapter");
        if (pagAdapter && class_getInstanceMethod(pagAdapter, NSSelectorFromString(@"presentFromRootViewController:")))
            AKSwizzleInstanceMethod(pagAdapter, NSSelectorFromString(@"presentFromRootViewController:"), (IMP)AKPangleAdapterPresent, NULL);

        for (NSString *n in @[@"PAGLInterstitialAd", @"PAGBannerAd"]) {
            Class c = AKClass(n);
            if (!c) continue;
            if (class_getInstanceMethod(c, NSSelectorFromString(@"presentFromRootViewController:")))
                AKSwizzleInstanceMethod(c, NSSelectorFromString(@"presentFromRootViewController:"), (IMP)AKPangleShow, NULL);
        }
        // 原生/信息流（PAGLNativeAd 没有 present，走 registerContainer:withClickableViews:）
        Class pagNative = AKClass(@"PAGLNativeAd");
        if (pagNative) {
            if (class_getInstanceMethod(pagNative, NSSelectorFromString(@"registerContainer:withClickableViews:")))
                AKSwizzleInstanceMethod(pagNative, NSSelectorFromString(@"registerContainer:withClickableViews:"),
                                        (IMP)AKPangleRegisterContainer, NULL);
        }
        Class pagNativeMgr = AKClass(@"PAGNativeAdsManager");
        if (pagNativeMgr) {
            if (class_getInstanceMethod(pagNativeMgr, NSSelectorFromString(@"loadAdDataWithCount:")))
                AKSwizzleInstanceMethod(pagNativeMgr, NSSelectorFromString(@"loadAdDataWithCount:"),
                                        (IMP)AKPangleLoadNative, NULL);
            if (class_getInstanceMethod(pagNativeMgr, NSSelectorFromString(@"loadAdDataWithCount:invokeByPublisher:")))
                AKSwizzleInstanceMethod(pagNativeMgr, NSSelectorFromString(@"loadAdDataWithCount:invokeByPublisher:"),
                                        (IMP)AKPangleRegisterContainer, NULL);
        }
        Class pagRewarded = AKClass(@"PAGNativeExpressRewardedVideoAd");
        if (pagRewarded) {
            if (class_getInstanceMethod(pagRewarded, NSSelectorFromString(@"showAdFromRootViewController:")))
                AKSwizzleInstanceMethod(pagRewarded, NSSelectorFromString(@"showAdFromRootViewController:"), (IMP)AKPangleShow, NULL);
            if (class_getInstanceMethod(pagRewarded, NSSelectorFromString(@"showAdFromRootViewController:ritScene:ritSceneDescribe:")))
                AKSwizzleInstanceMethod(pagRewarded, NSSelectorFromString(@"showAdFromRootViewController:ritScene:ritSceneDescribe:"), (IMP)AKPangleShowRit, NULL);
        }

#if AK_DISABLE_MEDIATION_INIT
        // 官方 API：关闭聚合中介初始化（Pangle/Vungle/Moloco/InMobi 适配器不再初始化）
        // 延后到主线程下一轮，确保 GADMobileAds 已完成类加载
        dispatch_async(dispatch_get_main_queue(), ^{
            @try {
                Class mobileAds = AKClass(@"GADMobileAds");
                if (mobileAds) {
                    id shared = [(id<AKGADMobileAdsShim>)mobileAds sharedInstance];
                    if ([shared respondsToSelector:@selector(disableMediationInitialization)]) {
                        [(id<AKGADMobileAdsShim>)shared disableMediationInitialization];
                        AK_LOG(@"mediation initialization disabled via GADMobileAds");
                    }
                }
            } @catch (NSException *e) { AK_LOG(@"disable mediation failed: %@", e); }
        });
#endif

        AK_LOG(@"=== AdsKiller ready ===");
    }
}
