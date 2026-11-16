package com.tvaitranslator;

import android.app.Service;
import android.content.Intent;
import android.net.Uri;
import android.os.IBinder;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.util.*;

public class ControlServerService extends Service {
    private volatile boolean running;
    private ServerSocket server;
    private Thread worker;
    private static final int PORT=3434;

    @Override public void onCreate(){super.onCreate();startServer();}
    private void startServer(){
        if(running)return; running=true;
        worker=new Thread(()->{
            try{
                server=new ServerSocket();
                server.setReuseAddress(true);
                server.bind(new InetSocketAddress("0.0.0.0",PORT));
                while(running){Socket client=server.accept();new Thread(()->handle(client)).start();}
            }catch(Exception e){android.util.Log.e("TVTranslator","Control server stopped",e);}
        },"tv-control-server");
        worker.start();
    }
    private void handle(Socket socket){
        try(Socket s=socket;
            BufferedReader in=new BufferedReader(new InputStreamReader(s.getInputStream(),"UTF-8"));
            OutputStream out=s.getOutputStream()){
            s.setSoTimeout(10000);
            String line=in.readLine();if(line==null)return;
            String[] bits=line.split(" ");String target=bits.length>1?bits[1]:"/";
            String line2;while((line2=in.readLine())!=null&&!line2.isEmpty()){}
            URI uri=new URI(target);String path=uri.getPath();Map<String,String> q=query(uri.getRawQuery());
            String body,type="application/json; charset=utf-8";int status=200;
            if("/".equals(path)){body=page();type="text/html; charset=utf-8";}
            else if("/api/search".equals(path)){body=search(q.get("q"));}
            else if("/api/offset".equals(path)){int ms=clamp(parseInt(q.get("ms"),0),-600000,600000);Intent i=new Intent(this,SubtitleOverlayService.class);i.setAction(SubtitleOverlayService.ACTION_OFFSET);i.putExtra(SubtitleOverlayService.EXTRA_OFFSET,ms);startService(i);SharedPreferences sp=getSharedPreferences("subtitle_profiles",MODE_PRIVATE);sp.edit().putInt(sp.getString("last_profile","default"),ms).apply();body="{\"ok\":true,\"offset\":"+ms+"}";}
            else if("/api/stop".equals(path)){stopService(new Intent(this,SubtitleOverlayService.class));body="{\"ok\":true}";}
            else if("/api/select".equals(path)){body=downloadAndStart(q.get("url"),q.get("name"));}
            else if("/api/ping".equals(path)){body="{\"ok\":true,\"port\":3434}";}
            else{status=404;body="{\"error\":\"not found\"}";}
            byte[] data=body.getBytes("UTF-8");
            String head="HTTP/1.1 "+status+" "+(status==200?"OK":"Not Found")+"\r\nContent-Type: "+type+"\r\nContent-Length: "+data.length+"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n";
            out.write(head.getBytes("UTF-8"));out.write(data);out.flush();
        }catch(Exception e){android.util.Log.w("TVTranslator","HTTP request failed",e);}
    }
    private String search(String title)throws Exception{
        if(title==null||title.trim().isEmpty())return "{\"error\":\"اكتب اسم الفيلم\"}";
        SharedPreferences p=getSharedPreferences("subtitle_profiles",MODE_PRIVATE);
        String key=p.getString("subdl_key","");
        if(key.isEmpty())return "{\"error\":\"افتح التطبيق على التلفزيون واحفظ مفتاح SubDL أولاً\"}";
        String url="https://api.subdl.com/api/v1/subtitles?api_key="+enc(key)+"&film_name="+enc(title)+"&languages=AR&subs_per_page=30&unpack=1";
        JSONObject root=getJson(url);JSONArray subs=root.optJSONArray("subtitles"),out=new JSONArray();
        if(subs!=null)for(int i=0;i<subs.length();i++){
            JSONObject sub=subs.optJSONObject(i);if(sub==null)continue;
            JSONArray files=sub.optJSONArray("unpack_files");if(files==null)continue;
            for(int j=0;j<files.length();j++){
                JSONObject f=files.optJSONObject(j);if(f==null)continue;
                JSONObject item=new JSONObject();item.put("name",f.optString("name","subtitle.srt"));
                item.put("release",sub.optString("release_name",sub.optString("name","")));
                item.put("url",f.optString("url",""));item.put("language",f.optString("language","AR"));
                out.put(item);
            }
        }
        JSONObject result=new JSONObject();result.put("ok",true);result.put("results",out);return result.toString();
    }
    private JSONObject getJson(String link)throws Exception{
        HttpURLConnection c=(HttpURLConnection)new URL(link).openConnection();c.setConnectTimeout(15000);c.setReadTimeout(20000);
        try{InputStream in=c.getResponseCode()<300?c.getInputStream():c.getErrorStream();String text=read(in);JSONObject j=new JSONObject(text);if(!j.optBoolean("status"))throw new IOException(j.optString("error","فشل البحث"));return j;}finally{c.disconnect();}
    }
    private String downloadAndStart(String path,String name)throws Exception{
        if(path==null||path.isEmpty())return "{\"error\":\"رابط الترجمة غير موجود\"}";
        if(!android.provider.Settings.canDrawOverlays(this))return "{\"error\":\"افتح التطبيق على التلفزيون واسمح بالظهور فوق التطبيقات أولاً\"}";
        String link=path.startsWith("http")?path:"https://dl.subdl.com"+(path.startsWith("/")?path:"/"+path);
        HttpURLConnection c=(HttpURLConnection)new URL(link).openConnection();c.setConnectTimeout(15000);c.setReadTimeout(30000);
        File file=new File(getCacheDir(),"remote_selected.srt");
        try{if(c.getResponseCode()<200||c.getResponseCode()>=300)throw new IOException("Download HTTP "+c.getResponseCode());try(InputStream in=c.getInputStream();FileOutputStream fos=new FileOutputStream(file)){byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1)fos.write(b,0,n);}}finally{c.disconnect();}
        Intent i=new Intent(this,SubtitleOverlayService.class);i.setAction(SubtitleOverlayService.ACTION_START);i.setData(Uri.fromFile(file));
        SharedPreferences sp=getSharedPreferences("subtitle_profiles",MODE_PRIVATE);
        i.putExtra(SubtitleOverlayService.EXTRA_OFFSET,sp.getInt(sp.getString("last_profile","default"),0));startService(i);
        JSONObject r=new JSONObject();r.put("ok",true);r.put("name",name==null?"subtitle.srt":name);return r.toString();
    }
    private String page(){return "<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>مترجم التلفزيون</title><style>body{font-family:Arial;background:#101827;color:#fff;margin:0;padding:18px}main{max-width:620px;margin:auto}section{background:#1d2b40;padding:16px;border-radius:14px;margin:12px 0}input,button{box-sizing:border-box;width:100%;padding:14px;margin:6px 0;border:0;border-radius:9px;font-size:17px}button{background:#16865b;color:white;font-weight:bold}.alt{background:#38506d}.danger{background:#a33}.result{background:#30435c;padding:12px;border-radius:9px;margin:8px 0}small{color:#bdc9d8}#msg{color:#ffd36a}</style><main><h2>مترجم التلفزيون</h2><small>تحكم محلي عبر شبكة Wi-Fi — المنفذ 3434</small><section><h3>البحث عن ترجمة عربية</h3><input id='title' placeholder='اسم الفيلم أو المسلسل'><button onclick='search()'>ابحث عن الترجمة</button><div id='msg'></div><div id='results'></div></section><section><h3>تقديم وتأخير الترجمة</h3><div id='val'>0 ثانية</div><input id='offset' type='range' min='-600' max='600' value='0' oninput='val.textContent=this.value+\" ثانية\"' onchange='setOffset(this.value*1000)'><button class='alt' onclick='setOffset(0);offset.value=0;val.textContent=\"0 ثانية\"'>إعادة للصفر</button></section><section><button class='danger' onclick='stopSub()'>إيقاف الترجمة</button></section></main><script>async function search(){msg.textContent='جاري البحث...';results.innerHTML='';try{let r=await fetch('/api/search?q='+encodeURIComponent(title.value));let j=await r.json();if(j.error){msg.textContent=j.error;return}msg.textContent='اختر ملف الترجمة المناسب';j.results.forEach(x=>{let d=document.createElement('div');d.className='result';let t=document.createElement('div');t.textContent=x.name+' — '+x.release;let b=document.createElement('button');b.textContent='تنزيل وتشغيل';b.onclick=async()=>{msg.textContent='جاري التنزيل والتشغيل...';let z=await fetch('/api/select?url='+encodeURIComponent(x.url)+'&name='+encodeURIComponent(x.name));let a=await z.json();msg.textContent=a.ok?'تم تشغيل الترجمة':(a.error||'تعذر التشغيل')};d.append(t,b);results.append(d)})}catch(e){msg.textContent='تعذر الاتصال بالجهاز'}}async function setOffset(ms){await fetch('/api/offset?ms='+ms)}async function stopSub(){await fetch('/api/stop');msg.textContent='تم إرسال أمر الإيقاف'}</script></html>";}
    private Map<String,String> query(String raw)throws Exception{Map<String,String> m=new HashMap<>();if(raw!=null)for(String part:raw.split("&")){String[] p=part.split("=",2);m.put(URLDecoder.decode(p[0],"UTF-8"),p.length>1?URLDecoder.decode(p[1],"UTF-8"):"");}return m;}
    private String enc(String s)throws Exception{return URLEncoder.encode(s,"UTF-8");}
    private String read(InputStream in)throws Exception{try(BufferedReader r=new BufferedReader(new InputStreamReader(in,"UTF-8"))){StringBuilder b=new StringBuilder();String l;while((l=r.readLine())!=null)b.append(l);return b.toString();}}
    private int parseInt(String s,int d){try{return Integer.parseInt(s);}catch(Exception e){return d;}}
    private int clamp(int n,int a,int b){return Math.max(a,Math.min(b,n));}
    @Override public void onDestroy(){running=false;try{if(server!=null)server.close();}catch(Exception ignored){}super.onDestroy();}
    @Override public IBinder onBind(Intent i){return null;}
}
