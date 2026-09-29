package app.smsledger;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.provider.Telephony;
import android.telephony.SmsMessage;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Every incoming SMS: bank transactions are queued and sent; everything else stays on the phone. */
public class SmsReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (!Telephony.Sms.Intents.SMS_RECEIVED_ACTION.equals(intent.getAction())) return;
        Store store = new Store(ctx);
        if (!store.connected()) return;
        SmsMessage[] parts = Telephony.Sms.Intents.getMessagesFromIntent(intent);
        if (parts == null) return;

        // a long SMS arrives in parts; join them per sender
        Map<String, StringBuilder> bySender = new LinkedHashMap<>();
        for (SmsMessage part : parts) {
            if (part == null) continue;
            String sender = String.valueOf(part.getDisplayOriginatingAddress());
            StringBuilder text = bySender.get(sender);
            if (text == null) bySender.put(sender, text = new StringBuilder());
            text.append(part.getDisplayMessageBody());
        }
        Pattern otp = Core.otp(store.otp());
        long now = System.currentTimeMillis();
        List<Queue.Item> items = new ArrayList<>();
        for (Map.Entry<String, StringBuilder> e : bySender.entrySet()) {
            String text = e.getValue().toString();
            if (Core.wanted(e.getKey(), text, otp)) items.add(new Queue.Item(Core.clip(text), now, "android"));
        }
        if (items.isEmpty()) return;

        PendingResult pending = goAsync();
        new Thread(() -> {
            try {
                store.queue().add(items);
            } catch (IOException ignored) {
                // storage full: nothing more to do here
            } finally {
                App.schedule(ctx);  // sent by the job as soon as there is internet
                pending.finish();
            }
        }, "smsledger-sms").start();
    }
}
