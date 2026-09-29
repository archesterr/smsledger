package app.smsledger;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.provider.Telephony;
import android.text.format.DateUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/**
 * The one screen: connection, SMS permission, battery, what is waiting to be sent, and old SMS.
 * The setup page's Connect button opens it with smsledger://connect?server=…&key=….
 */
public class MainActivity extends Activity {
    private static final int REQ_SMS = 1;
    private static final int TEAL = Color.rgb(15, 118, 110);
    private final Handler ui = new Handler(Looper.getMainLooper());
    private LinearLayout page;
    private Store store;
    private String importStatus = "";
    private boolean importing;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = new Store(this);
        ScrollView scroll = new ScrollView(this);
        scroll.setLayoutDirection(View.LAYOUT_DIRECTION_RTL);
        scroll.setBackgroundColor(Color.rgb(246, 247, 249));
        page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        page.setPadding(pad, pad, pad, pad);
        scroll.addView(page);
        // Android 15 draws apps under the status and navigation bars: keep the content clear of them
        scroll.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(0, insets.getSystemWindowInsetTop(), 0, insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(scroll);
        if (state == null) handleLink(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleLink(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        render();
        if (store.connected()) {
            refreshConfig();
            if (store.queue().size() > 0) sendNow();
        }
    }

    // ---- connecting -----------------------------------------------------------------------------
    private void handleLink(Intent intent) {
        Uri uri = intent == null ? null : intent.getData();
        if (uri == null || !"smsledger".equals(uri.getScheme()) || !"connect".equals(uri.getHost())) return;
        setIntent(new Intent(this, MainActivity.class));  // not again after a rotation
        String base = Core.serverBase(uri.getQueryParameter("server"));
        String key = uri.getQueryParameter("key");
        if (base == null || !Core.isKey(key)) {
            alert("لینک اتصال درست نیست", "در سایت، صفحه «راه‌اندازی گوشی» را باز کنید و دوباره دکمه «اتصال» را بزنید.");
            return;
        }
        String host = Core.host(base);
        String msg = "پیامک‌های بانکی این گوشی به این سایت فرستاده می‌شوند:\n\n" + host
                + "\n\nفقط اگر همین سایتی است که در آن حساب دارید، «وصل شو» را بزنید.";
        if (store.connected() && !store.base().equals(base)) {
            msg += "\n\n⚠️ این گوشی الان به سایت دیگری (" + Core.host(store.base()) + ") وصل است و از آن جدا می‌شود.";
        }
        new AlertDialog.Builder(this).setTitle("اتصال به " + host).setMessage(msg)
                .setPositiveButton("وصل شو", (d, w) -> connect(base, key))
                .setNegativeButton("نه", null).show();
    }

    private void connect(String base, String key) {
        toast("در حال اتصال…");
        background(() -> {
            String title, text;
            boolean ok = false;
            try {
                Api.Reply r = new Api(base, key, App.agent(this)).connect();
                ok = r.ok();
                if (ok) {
                    store.connect(base, key, r.json.optString("account", ""));
                    title = "✅ وصل شد";
                    text = r.message();
                } else {
                    title = "وصل نشد";
                    text = r.message().isEmpty() ? "سرور جواب داد: " + r.code : r.message();
                }
            } catch (IOException e) {
                title = "وصل نشد";
                text = "به سرور نرسیدیم. اینترنت گوشی را بررسی کنید و دوباره از سایت «اتصال» را بزنید.";
            }
            boolean connected = ok;
            String t = title, m = text;
            ui.post(() -> {
                if (isDestroyed()) return;
                render();
                new AlertDialog.Builder(this).setTitle(t).setMessage(m)
                        .setPositiveButton("باشه", (d, w) -> {
                            if (connected && !hasSms()) askSms();
                        }).show();
                if (connected) refreshConfig();
            });
        });
    }

    private void refreshConfig() {
        background(() -> {
            try {
                Api.Reply r = store.api().config();
                if (r.code == 200) {
                    JSONArray periods = r.json.optJSONArray("periods");
                    store.setConfig(r.json.optString("otp", ""), periods == null ? "" : periods.toString());
                    ui.post(this::render);
                }
            } catch (IOException ignored) {
                // offline: the saved settings still work
            }
        });
    }

    // ---- permissions ----------------------------------------------------------------------------
    private boolean hasSms() {
        return checkSelfPermission(Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.READ_SMS) == PackageManager.PERMISSION_GRANTED;
    }

    private void askSms() {
        store.setAskedSms();
        requestPermissions(new String[]{Manifest.permission.RECEIVE_SMS, Manifest.permission.READ_SMS}, REQ_SMS);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        render();
    }

    /** Asked before and Android no longer shows the question: only the settings screen can allow it. */
    private boolean smsBlocked() {
        return store.askedSms() && !shouldShowRequestPermissionRationale(Manifest.permission.RECEIVE_SMS);
    }

    private boolean batteryOk() {
        return getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(getPackageName());
    }

    // ---- sending --------------------------------------------------------------------------------
    private void sendNow() {
        background(() -> {
            App.flush(this, (count, left) -> ui.post(this::render));
            ui.post(this::render);
        });
    }

    private void importOld(long start) {
        importing = true;
        importStatus = "در حال خواندن پیامک‌ها…";
        render();
        background(() -> {
            Pattern otp = Core.otp(store.otp());
            List<Queue.Item> found = new ArrayList<>();
            int scanned = 0;
            String[] cols = {Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE};
            try (Cursor c = getContentResolver().query(Telephony.Sms.Inbox.CONTENT_URI, cols,
                    Telephony.Sms.DATE + " >= ?", new String[]{Long.toString(start)}, Telephony.Sms.DATE + " ASC")) {
                while (c != null && c.moveToNext()) {
                    scanned++;
                    String text = c.getString(1);
                    if (Core.wanted(c.getString(0), text, otp)) {
                        found.add(new Queue.Item(Core.clip(text), c.getLong(2), "android-import"));
                    }
                }
                store.queue().add(found);
            } catch (IOException | RuntimeException e) {
                finishImport("خواندن پیامک‌ها ممکن نشد: " + e.getMessage());
                return;
            }
            int total = found.size();
            String head = Core.fa(scanned) + " پیامک خوانده شد؛ " + Core.fa(total) + " پیامک بانکی پیدا شد.";
            if (total == 0) {
                finishImport(head);
                return;
            }
            ui.post(() -> {
                importStatus = head + "\nدر حال ارسال…";
                render();
            });
            Sender.Outcome out = App.flush(this, (count, left) -> ui.post(() -> {
                importStatus = head + "\nدر حال ارسال… " + Core.fa(left) + " مانده";
                render();
            }));
            finishImport(head + "\n" + (out == Sender.Outcome.DONE
                    ? "✅ همه فرستاده شد. تراکنش‌ها در سایت هستند (پیامک تکراری دوباره ثبت نمی‌شود)."
                    : out == Sender.Outcome.REVOKED ? "❌ کلید این گوشی باطل شده؛ از سایت دوباره «اتصال» را بزنید."
                    : "اینترنت قطع شد؛ بقیه خودکار فرستاده می‌شود."));
        });
    }

    private void finishImport(String status) {
        ui.post(() -> {
            importing = false;
            importStatus = status;
            render();
        });
    }

    // ---- the screen -----------------------------------------------------------------------------
    private void render() {
        page.removeAllViews();
        TextView title = text("دخل و خرج", 22, true);
        title.setTextColor(TEAL);
        page.addView(title);
        page.addView(text("پیامک‌های بانکی این گوشی را خودکار به حسابتان می‌فرستد.", 14, false));

        LinearLayout c = card();
        if (!store.connected()) {
            c.addView(text("۱. این گوشی هنوز وصل نشده", 17, true));
            c.addView(text("در مرورگر همین گوشی وارد سایت شوید، «بیشتر ← راه‌اندازی گوشی» را باز کنید و دکمه «اتصال» را بزنید.", 15, false));
            return;
        }
        if (store.revoked()) {
            c.addView(text("❌ کلید این گوشی باطل شده", 17, true));
            c.addView(text("در سایت «بیشتر ← راه‌اندازی گوشی» را باز کنید و دوباره «اتصال» را بزنید. پیامک‌های در صف نگه داشته شده‌اند.", 15, false));
            c.addView(button("باز کردن سایت", v -> openSite("setup/")));
        } else {
            c.addView(text("✅ وصل است", 17, true));
            String account = store.account().isEmpty() ? "" : "حساب «" + store.account() + "» در ";
            c.addView(text(account + Core.host(store.base()), 15, false));
        }

        if (!hasSms()) {
            LinearLayout p = card();
            p.addView(text("⚠️ اجازه پیامک داده نشده", 17, true));
            if (smsBlocked()) {
                p.addView(text("اندروید دیگر سؤال نمی‌پرسد. در تنظیمات برنامه:\n"
                        + "۱. اگر نوشته «تنظیمات محدودشده» (Restricted setting)، بالای صفحه روی ⋮ بزنید و «اجازه دادن به تنظیمات محدودشده» (Allow restricted settings) را بزنید.\n"
                        + "۲. «مجوزها» (Permissions) ← «پیامک» (SMS) ← «اجازه دادن» (Allow).\n"
                        + "بعد به این برنامه برگردید.", 15, false));
                p.addView(button("باز کردن تنظیمات برنامه", v -> startActivity(new Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())))));
            } else {
                p.addView(text("برای اینکه پیامک‌های بانک خوانده شوند، دکمه را بزنید و «اجازه دادن» (Allow) را انتخاب کنید. پیامک‌های شخصی و رمزهای یک‌بارمصرف هرگز فرستاده نمی‌شوند.", 15, false));
                p.addView(button("اجازه دسترسی به پیامک", v -> askSms()));
            }
        }

        if (!batteryOk()) {
            LinearLayout b = card();
            b.addView(text("🔋 صرفه‌جویی باتری", 17, true));
            b.addView(text("بعضی گوشی‌ها برنامه‌های پس‌زمینه را می‌بندند و پیامک دیر می‌رسد. دکمه را بزنید و «اجازه دادن» (Allow) را انتخاب کنید.", 15, false));
            b.addView(button("اجازه کار در پس‌زمینه", v -> askBattery()));
        }

        LinearLayout s = card();
        int queued = store.queue().size();
        s.addView(text("وضعیت ارسال", 17, true));
        String sent = store.lastSent() == 0 ? "هنوز پیامکی فرستاده نشده."
                : "آخرین ارسال: " + DateUtils.getRelativeTimeSpanString(store.lastSent()) + " · جمعاً " + Core.fa(store.sentTotal()) + " پیامک";
        s.addView(text(sent, 15, false));
        if (queued > 0) {
            s.addView(text(Core.fa(queued) + " پیامک منتظر اینترنت است.", 15, false));
            s.addView(button("ارسال الان", v -> sendNow()));
        }

        if (hasSms() && !store.revoked()) {
            LinearLayout o = card();
            o.addView(text("پیامک‌های قدیمی", 17, true));
            o.addView(text("پیامک‌های بانکی که قبلاً آمده‌اند را هم بفرستید. هر چند بار بزنید، تکراری ثبت نمی‌شود.", 15, false));
            if (importing) {
                o.addView(text(importStatus, 15, true));
            } else {
                for (JSONObject p : periods()) {
                    String label = p.optString("label") + (p.optString("hint").isEmpty() ? "" : " (" + p.optString("hint") + ")");
                    long start = p.optLong("start");
                    o.addView(button(label, v -> importOld(start)));
                }
                if (!importStatus.isEmpty()) o.addView(text(importStatus, 15, false));
            }
        }

        Button site = button("باز کردن سایت", v -> openSite(""));
        page.addView(site);
    }

    private List<JSONObject> periods() {
        List<JSONObject> out = new ArrayList<>();
        try {
            JSONArray a = new JSONArray(store.periods());
            for (int i = 0; i < a.length(); i++) out.add(a.getJSONObject(i));
        } catch (JSONException e) {
            try {
                out.add(new JSONObject().put("label", "همه پیامک‌ها").put("start", 0));
            } catch (JSONException ignored) {
                // a literal can't fail
            }
        }
        return out;
    }

    private void askBattery() {
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (RuntimeException e) {  // some phones hide the dialog: open the list instead
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        }
    }

    private void openSite(String path) {
        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(store.base() + path)));
    }

    // ---- small helpers --------------------------------------------------------------------------
    private void background(Runnable r) {
        new Thread(r, "smsledger-ui").start();
    }

    private void alert(String title, String msg) {
        new AlertDialog.Builder(this).setTitle(title).setMessage(msg).setPositiveButton("باشه", null).show();
    }

    private void toast(String msg) {
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show();
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private LinearLayout card() {
        LinearLayout c = new LinearLayout(this);
        c.setOrientation(LinearLayout.VERTICAL);
        c.setPadding(dp(16), dp(12), dp(16), dp(12));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.WHITE);
        bg.setCornerRadius(dp(12));
        c.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(12);
        page.addView(c, lp);
        return c;
    }

    private TextView text(String s, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(Color.rgb(17, 17, 17));
        t.setGravity(Gravity.START);
        t.setTextDirection(View.TEXT_DIRECTION_RTL);
        t.setLineSpacing(0, 1.2f);
        t.setPadding(0, dp(4), 0, dp(4));
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private Button button(String label, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(TEAL);
        bg.setCornerRadius(dp(10));
        b.setBackground(bg);
        b.setOnClickListener(click);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(8);
        b.setLayoutParams(lp);
        return b;
    }
}
