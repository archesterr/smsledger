package app.smsledger;

import java.net.URI;
import java.util.regex.Pattern;

/** What leaves the phone. Plain Java (no Android classes), so it is unit-tested on the JVM. */
public final class Core {
    private Core() {}

    /** Same as the server's shortcut.OTP_PATTERN (a server test keeps them in step). */
    public static final String OTP_PATTERN = "رمز(?!\\s*ارز)|یک.?بار|کد.?(تایید|تأیید|ورود|فعال|پویا)|OTP";
    /** Every bank's transaction SMS has a balance line (the server's ingest.RE_BALANCE). */
    static final Pattern BALANCE = Pattern.compile("موجود[یي]|مانده");
    /** Iranian mobile numbers are people, not banks (sync_client.py's MOBILE). */
    static final Pattern MOBILE = Pattern.compile("^(\\+?98|0098|0)?9\\d{9}$");
    static final String TOKEN_PREFIX = "sml_";
    public static final int MAX_CHARS = 2000;

    public static Pattern otp(String pattern) {
        int flags = Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE;  // Android's regex (ICU) treats \s as Unicode already
        try {
            return Pattern.compile(pattern == null || pattern.isEmpty() ? OTP_PATTERN : pattern, flags);
        } catch (RuntimeException e) {  // a bad pattern from the server never stops the filter
            return Pattern.compile(OTP_PATTERN, flags);
        }
    }

    /** A mobile number or an email: someone who mentioned a balance, not a bank. */
    public static boolean isPerson(String sender) {
        if (sender == null) return false;
        return MOBILE.matcher(sender.replaceAll("[\\s-]", "")).matches() || sender.contains("@");
    }

    /** Only bank transaction SMS are sent: never people's messages, never one-time codes. */
    public static boolean wanted(String sender, String text, Pattern otp) {
        if (text == null || text.trim().isEmpty() || isPerson(sender)) return false;
        return BALANCE.matcher(text).find() && !otp.matcher(text).find();
    }

    public static String clip(String text) {
        String t = text.trim();
        return t.length() > MAX_CHARS ? t.substring(0, MAX_CHARS) : t;
    }

    /** The server's base URL (https://host/) from a connect link, or null if it isn't one. */
    public static String serverBase(String server) {
        if (server == null) return null;
        try {
            URI u = new URI(server.trim());
            if (!"https".equalsIgnoreCase(u.getScheme()) || u.getHost() == null || u.getRawUserInfo() != null) {
                return null;
            }
            return "https://" + u.getHost().toLowerCase() + (u.getPort() > 0 ? ":" + u.getPort() : "") + "/";
        } catch (Exception e) {
            return null;
        }
    }

    public static boolean isKey(String key) {
        return key != null && key.startsWith(TOKEN_PREFIX) && key.length() <= 100 && key.matches("[A-Za-z0-9_-]+");
    }

    public static String host(String base) {
        try {
            return new URI(base).getHost();
        } catch (Exception e) {
            return base;
        }
    }

    private static final char[] FA = "۰۱۲۳۴۵۶۷۸۹".toCharArray();

    public static String fa(long n) {
        StringBuilder s = new StringBuilder();
        for (char c : Long.toString(n).toCharArray()) s.append(c >= '0' && c <= '9' ? FA[c - '0'] : c);
        return s.toString();
    }
}
