document.body.addEventListener("htmx:configRequest", function (event) {
  const token = document.querySelector('meta[name="csrf-token"]');
  if (token) event.detail.headers["X-CSRF-Token"] = token.content;
});
(function(){
  var form=document.getElementById("traktImportForm");
  if(form){
    var box=document.getElementById("traktImportProgress"),bar=document.getElementById("traktImportBar"),pct=document.getElementById("traktImportPercent"),phase=document.getElementById("traktImportPhase"),msg=document.getElementById("traktImportMessage"),button=document.getElementById("traktImportButton");
    function show(p,m,s){p=Math.max(0,Math.min(100,p));box.classList.remove("d-none");bar.style.width=p+"%";bar.setAttribute("aria-valuenow",p);pct.textContent=p+"%";msg.textContent=m;phase.textContent=s;}
    function poll(url){fetch(url,{headers:{"Accept":"application/json"}}).then(function(r){return r.json();}).then(function(data){show(data.percent,data.message,data.state==="processing"?"Processando arquivo":data.state==="succeeded"?"Importação concluída":"Importação falhou");if(data.active){setTimeout(function(){poll(url);},750);}else{bar.classList.remove("progress-bar-animated");button.disabled=false;}}).catch(function(){show(parseInt(pct.textContent)||0,"Não foi possível atualizar o status; tentando novamente…","Conexão temporariamente interrompida");setTimeout(function(){poll(url);},1500);});}
    form.addEventListener("submit",function(e){e.preventDefault();if(!form.reportValidity())return;button.disabled=true;show(0,"Enviando o arquivo para o servidor…","Upload");var xhr=new XMLHttpRequest();xhr.open("POST",form.action);xhr.setRequestHeader("Accept","application/json");xhr.upload.onprogress=function(ev){if(ev.lengthComputable)show(Math.round(ev.loaded*100/ev.total),"Enviados "+Math.round(ev.loaded/1048576)+" de "+Math.round(ev.total/1048576)+" MiB","Upload");};xhr.onload=function(){var data;try{data=JSON.parse(xhr.responseText);}catch(err){show(100,"O servidor devolveu uma resposta inesperada.","Falha");button.disabled=false;return;}if(xhr.status===202){show(1,"Upload concluído. Iniciando validação…","Processando arquivo");poll(data.status_url);}else{show(100,data.error||"Não foi possível iniciar a importação.","Falha");button.disabled=false;}};xhr.onerror=function(){show(0,"Falha de rede durante o upload. Você pode tentar novamente.","Upload interrompido");button.disabled=false;};xhr.send(new FormData(form));});
  }
})();
(function(){
  var btn=document.getElementById("themeToggle"),key="euvieouvi.theme";
  function apply(t){document.documentElement.setAttribute("data-theme",t);document.documentElement.style.colorScheme=t;try{localStorage.setItem(key,t)}catch(e){} if(btn) btn.textContent=t==="dark"?"◑":"◐";}
  if(btn){btn.addEventListener("click",function(){var cur=document.documentElement.getAttribute("data-theme")==="dark"?"dark":"light";apply(cur==="dark"?"light":"dark");}); var cur=document.documentElement.getAttribute("data-theme"); if(cur) apply(cur);}
  var form=document.getElementById("appearanceForm");
  if(form) form.addEventListener("submit",function(){var selected=form.querySelector('input[name="theme"]:checked');try{if(selected&&selected.value==="system")localStorage.removeItem(key);else if(selected)localStorage.setItem(key,selected.value);}catch(e){}});
})();
