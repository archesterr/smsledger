package app.smsledger;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;

/** This phone's settings. The key stays in app-private storage (backups are off in the manifest). */
final class Store {
    private final SharedPreferences p;
    private final Context ctx;

    Store(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.p = this.ctx.getSharedPreferences("smsledger", Context.MODE_PRIVATE);
    }

    String base() { return p.getString("base", ""); }
    String key() { return p.getString("key", ""); }
    String account() { return p.getString("account", ""); }
    boolean connected() { return !base().isEmpty() && !key().isEmpty(); }
    boolean revoked() { return p.getBoolean("revoked", false); }
    String otp() { return p.getString("otp", Core.OTP_PATTERN); }
    String periods() { return p.getString("periods", ""); }
    long lastSent() { return p.getLong("last_sent", 0); }
    long sentTotal() { return p.getLong("sent_total", 0); }
    boolean askedSms() { return p.getBoolean("asked_sms", false); }

    void connect(String base, String key, String account) {
        p.edit().putString("base", base).putString("key", key).putString("account", account)
                .putBoolean("revoked", false).apply();
    }

    void setRevoked() { p.edit().putBoolean("revoked", true).apply(); }
    void setAskedSms() { p.edit().putBoolean("asked_sms", true).apply(); }

    void setConfig(String otp, String periods) {
        p.edit().putString("otp", otp.isEmpty() ? Core.OTP_PATTERN : otp).putString("periods", periods).apply();
    }

    void addSent(int n) {
        p.edit().putLong("last_sent", System.currentTimeMillis()).putLong("sent_total", sentTotal() + n).apply();
    }

    Queue queue() {
        return new Queue(new File(ctx.getFilesDir(), "queue.jsonl"));
    }

    Api api() {
        return new Api(base(), key(), App.agent(ctx));
    }
}
