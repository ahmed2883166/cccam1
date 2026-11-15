package com.tvaitranslator;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.widget.*;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final int PICK_SRT=41, MAX_OFFSET_MS=600000;
    private SharedPreferences prefs;
    private EditText titleInput,keyInput,profileInput;
    private TextView status,offsetLabel,fileLabel;
    private LinearLayout resultsBox;
    private SeekBar offsetSlider;
    private Uri selectedSrt;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs=getSharedPreferences("subtitle_profiles",MODE_PRIVATE);\n        startService(new Intent(this, ControlServerService.class));
        ScrollView scroll=new ScrollView(this);
        LinearLayout root=new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(22,18,22,24); root.setBackgroundColor(Color.rgb(18,22,30)); scroll.addView(root);
        root.addView(label("مترجم التلفزيون",25,Color.WHITE));
        root.addView(label("ابحث عن ترجمة عربية للفيلم أو المسلسل وشغّلها فوق DVB Player",15,Color.LTGRAY));\n        root.addView(label("لوحة التحكم من الموبايل: http://IP-الجهاز:3434 (نفس شبكة Wi-Fi)",14,Color.YELLOW));

        titleInput=edit("اكتب اسم الفيلم أو المسلسل"); root.addView(titleInput);
        keyInput=edit("مفتاح SubDL API"); keyInput.setInputType( android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
        keyInput.setText(prefs.getString("subdl_key","")); root.addView(keyInput);
        Button search=button("ابحث عن ترجمة عربية"); root.addView(search);
        search.setOnClickListener(v->searchSubtitles());

        status=label("اكتب اسم العمل ومفتاح SubDL للبحث. يمكنك الحصول على مفتاح مجاني من موقع SubDL.",14,Color.YELLOW); root.addView(status);
        resultsBox=new LinearLayout(this); resultsBox.setOrientation(LinearLayout.VERTICAL); root.addView(resultsBox);

        root.addView(label("بحث يدوي بديل: اختيار ملف SRT من الجهاز",15,Color.WHITE));
        Button choose=button("اختيار ملف ترجمة SRT"); root.addView(choose);
        choose.setOnClickListener(v->{Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT);i.addCategory(Intent.CATEGORY_OPENABLE);i.setType("*/*");i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);startActivityForResult(i,PICK_SRT);});
        fileLabel=label("لم يتم اختيار ملف",13,Color.LTGRAY); root.addView(fileLabel);
        Button play=button("تشغيل الترجمة فوق المشغل");root.addView(play);play.setOnClickListener(v->startOverlay());
        Button stop=button("إيقاف الترجمة العائمة");root.addView(stop);stop.setOnClickListener(v->stopService(new Intent(this,SubtitleOverlayService.class)));

        root.addView(label("اسم الفيلم/القناة لحفظ ضبط التوقيت",14,Color.LTGRAY));
        profileInput=edit("اسم الملف أو القناة");root.addView(profileInput);
        offsetLabel=label("التوقيت: 0.0 ثانية",17,Color.WHITE);root.addView(offsetLabel);
        offsetSlider=new SeekBar(this);offsetSlider.setMax(1200);root.addView(offsetSlider);
        offsetSlider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){
            public void onProgressChanged(SeekBar b,int p,boolean user){int ms=(p-600)*1000;showOffset(ms);prefs.edit().putInt(profileKey(),ms).apply();Intent u=new Intent(MainActivity.this,SubtitleOverlayService.class);u.setAction(SubtitleOverlayService.ACTION_OFFSET);u.putExtra(SubtitleOverlayService.EXTRA_OFFSET,ms);startService(u);}
            public void onStartTrackingTouch(SeekBar b){} public void onStopTrackingTouch(SeekBar b){}
        });
        Button reset=button("إعادة التوقيت للصفر");root.addView(reset);reset.setOnClickListener(v->offsetSlider.setProgress(600));
        profileInput.setText("عام"); loadProfile();
        setContentView(scroll);
    }

    private void searchSubtitles(){
        final String title=titleInput.getText().toString().trim(), key=keyInput.getText().toString().trim();
        if(title.isEmpty()||key.isEmpty()){status.setText("اكتب اسم الفيلم ومفتاح SubDL أولًا.");return;}
        prefs.edit().putString("subdl_key",key).apply();
        status.setText("جاري البحث عن الترجمات العربية...");
        resultsBox.removeAllViews();
        new Thread(()->{
            try{
                String q="https://api.subdl.com/api/v1/subtitles?api_key="+enc(key)+"&film_name="+enc(title)+"&languages=AR&subs_per_page=30&unpack=1";
                HttpURLConnection c=(HttpURLConnection)new URL(q).openConnection();c.setConnectTimeout(15000);c.setReadTimeout(20000);c.setRequestProperty("Accept","application/json");
                int code=c.getResponseCode();InputStream in=code>=200&&code<300?c.getInputStream():c.getErrorStream();
                String body=read(in);c.disconnect();JSONObject data=new JSONObject(body);
                if(!data.optBoolean("status"))throw new Exception(data.optString("error","فشل البحث من SubDL"));
                JSONArray subs=data.optJSONArray("subtitles"); final int count=subs==null?0:subs.length();
                runOnUiThread(()->{
                    if(count==0){status.setText("لم تظهر ترجمة عربية لهذا الاسم. جرّب كتابة الاسم بالإنجليزية أو اسم الحلقة.");return;}
                    status.setText("نتائج الترجمة العربية: "+count+" — اختر الملف المناسب.");
                    for(int i=0;i<count;i++){
                        JSONObject sub=subs.optJSONObject(i);if(sub==null)continue;
                        JSONArray files=sub.optJSONArray("unpack_files");
                        if(files!=null&&files.length()>0){
                            for(int j=0;j<files.length();j++) addSubtitleChoice(files.optJSONObject(j),sub.optString("release_name",sub.optString("name","ترجمة")));
                        } else {
                            final String zip=sub.optString("url","");
                            TextView note=label("حزمة غير مفكوكة: "+sub.optString("release_name",sub.optString("name","ملف"))+" (تنزيل ZIP غير مدعوم بعد)",13,Color.LTGRAY);resultsBox.addView(note);
                        }
                    }
                });
            }catch(Exception e){runOnUiThread(()->status.setText("تعذر البحث: "+e.getMessage()));}
        }).start();
    }

    private void addSubtitleChoice(JSONObject f,String parent){
        if(f==null)return;
        String name=f.optString("name","subtitle.srt"),release=f.optString("release_name",parent);
        String lang=f.optString("language","AR"),format=f.optString("format","srt"),url=f.optString("url","");
        TextView item=label(name+"\n"+release+" | "+lang+" | "+format,14,Color.WHITE);resultsBox.addView(item);
        Button download=button("تنزيل وتشغيل: "+name);resultsBox.addView(download);
        download.setOnClickListener(v->downloadAndPlay(url,name));
    }

    private void downloadAndPlay(String path,String name){
        if(path.isEmpty()){status.setText("رابط ملف الترجمة غير متاح.");return;}
        status.setText("جاري تنزيل ملف الترجمة...");
        new Thread(()->{
            try{
                String link=path.startsWith("http")?path:"https://dl.subdl.com"+(path.startsWith("/")?path:"/"+path);
                HttpURLConnection c=(HttpURLConnection)new URL(link).openConnection();c.setConnectTimeout(15000);c.setReadTimeout(30000);
                if(c.getResponseCode()<200||c.getResponseCode()>=300)throw new IOException("HTTP "+c.getResponseCode());
                File out=new File(getCacheDir(),"selected_subtitle.srt");
                try(InputStream in=c.getInputStream();FileOutputStream fos=new FileOutputStream(out)){byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1)fos.write(b,0,n);}
                c.disconnect();
                runOnUiThread(()->{selectedSrt=Uri.fromFile(out);fileLabel.setText("تم التنزيل: "+name);status.setText("تم التنزيل. اضغط تشغيل الترجمة فوق المشغل.");startOverlay();});
            }catch(Exception e){runOnUiThread(()->status.setText("فشل تنزيل الترجمة: "+e.getMessage()));}
        }).start();
    }

    private void startOverlay(){
        if(selectedSrt==null){status.setText("اختار أو نزّل ملف SRT أولًا.");return;}
        if(!Settings.canDrawOverlays(this)){Intent p=new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,Uri.parse("package:"+getPackageName()));startActivity(p);status.setText("اسمح بالظهور فوق التطبيقات ثم اضغط تشغيل مرة أخرى.");return;}
        Intent s=new Intent(this,SubtitleOverlayService.class);s.setAction(SubtitleOverlayService.ACTION_START);s.setData(selectedSrt);s.putExtra(SubtitleOverlayService.EXTRA_OFFSET,prefs.getInt(profileKey(),0));startService(s);
        status.setText("تم إرسال ملف الترجمة إلى الطبقة العائمة.");
    }

    @Override protected void onActivityResult(int r,int code,Intent data){super.onActivityResult(r,code,data);if(r==PICK_SRT&&code==RESULT_OK&&data!=null){selectedSrt=data.getData();try{getContentResolver().takePersistableUriPermission(selectedSrt,Intent.FLAG_GRANT_READ_URI_PERMISSION);}catch(Exception ignored){}fileLabel.setText("ملف الترجمة: "+selectedSrt.getLastPathSegment());}}
    private String profileKey(){String s=profileInput==null?"":profileInput.getText().toString().trim();return s.isEmpty()?"default":s;}
    private void loadProfile(){int ms=prefs.getInt(profileKey(),0);offsetSlider.setProgress(Math.max(0,Math.min(1200,600+ms/1000)));showOffset(ms);}
    private void showOffset(int ms){offsetLabel.setText((ms>0?"تأخير":ms<0?"تقديم":"بدون إزاحة")+String.format(Locale.US,": %+.1f ثانية",ms/1000.0));}
    private String enc(String s)throws Exception{return URLEncoder.encode(s,"UTF-8");}
    private String read(InputStream in)throws Exception{try(BufferedReader r=new BufferedReader(new InputStreamReader(in,"UTF-8"))){StringBuilder b=new StringBuilder();String l;while((l=r.readLine())!=null)b.append(l);return b.toString();}}
    private EditText edit(String hint){EditText e=new EditText(this);e.setSingleLine(true);e.setTextColor(Color.WHITE);e.setHintTextColor(Color.GRAY);e.setHint(hint);return e;}
    private TextView label(String s,int size,int color){TextView t=new TextView(this);t.setText(s);t.setTextSize(size);t.setTextColor(color);t.setPadding(0,9,0,9);return t;}
    private Button button(String s){Button b=new Button(this);b.setText(s);return b;}
}
