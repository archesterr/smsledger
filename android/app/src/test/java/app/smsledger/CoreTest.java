package app.smsledger;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.regex.Pattern;

import org.junit.Test;

public class CoreTest {
    private static final Pattern OTP = Core.otp(null);
    private static final String TX = "بانک سامان\nبرداشت: 1,250,000\nمانده: 8,400,000\n0412-13:05";

    @Test
    public void bankTransactionsAreSent() {
        assertTrue(Core.wanted("SamanBank", TX, OTP));
        assertTrue(Core.wanted("+983000", "انتقال 500,000 موجودی 2,000", OTP));
        assertTrue(Core.wanted(null, TX, OTP));
    }

    @Test
    public void peopleAdsAndCodesAreNot() {
        assertFalse(Core.wanted("09121234567", TX, OTP));
        assertFalse(Core.wanted("+98 912 123 4567", TX, OTP));
        assertFalse(Core.wanted("friend@icloud.com", TX, OTP));
        assertFalse(Core.wanted("SamanBank", "جشنواره قرعه‌کشی بانک سامان", OTP));
        assertFalse(Core.wanted("SamanBank", "رمز پویا: 123456\nمانده حساب شما", OTP));
        assertFalse(Core.wanted("SamanBank", "Your OTP is 5555 مانده", OTP));
        assertFalse(Core.wanted("SamanBank", "کد تایید 4321 مانده", OTP));
        assertFalse(Core.wanted("SamanBank", "  ", OTP));
    }

    @Test
    public void cryptoIsNotAPassword() {
        assertTrue(Core.wanted("Bank", "خرید رمز ارز 100 مانده 50", OTP));
    }

    @Test
    public void aBrokenServerPatternFallsBack() {
        assertTrue(Core.otp("(").matcher("رمز پویا").find());
    }

    @Test
    public void onlyHttpsServers() {
        assertEquals("https://tx.example.ir/", Core.serverBase("https://TX.example.ir/setup/?x=1"));
        assertEquals("https://tx.example.ir:8443/", Core.serverBase("https://tx.example.ir:8443"));
        assertNull(Core.serverBase("http://tx.example.ir/"));
        assertNull(Core.serverBase("https://user@evil.example/"));
        assertNull(Core.serverBase("javascript:alert(1)"));
        assertNull(Core.serverBase(null));
    }

    @Test
    public void keys() {
        assertTrue(Core.isKey("sml_abcDEF123-_x"));
        assertFalse(Core.isKey("abc"));
        assertFalse(Core.isKey("sml_a b"));
        assertFalse(Core.isKey(null));
    }

    @Test
    public void persianDigits() {
        assertEquals("۱۲۰", Core.fa(120));
    }
}
