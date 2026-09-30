document.body.addEventListener("htmx:configRequest", function (event) {
  const token = document.querySelector('meta[name="csrf-token"]');
  if (token) event.detail.headers["X-CSRF-Token"] = token.content;
});
(function(){
  var notifications=document.querySelectorAll(".toast");
  notifications.forEach(function(element){
    bootstrap.Toast.getOrCreateInstance(element).show();
  });
  if(notifications.length){
    document.addEventListener("click",function(event){
      if(event.target instanceof Element&&event.target.closest(".toast"))return;
      notifications.forEach(function(element){
        bootstrap.Toast.getOrCreateInstance(element).hide();
      });
    });
  }
})();
(function(){
  var form=document.getElementById("jobs-config");
  var dashboard=document.getElementById("dashboard-jobs");
  var eventUrl=form?form.dataset.jobsEventsUrl:dashboard?dashboard.dataset.jobsEventsUrl:null;
  var statusUrl=form?form.dataset.jobsStatusUrl:dashboard?dashboard.dataset.jobsStatusUrl:null;
  if(!eventUrl)return;
  var stopped=false,source=null,errorCount=0;
  function apply(data){
    if(form&&data.jobs){
      data.jobs.forEach(function(job){
        var status=document.getElementById("job-status-"+job.id),lastRun=document.getElementById("job-last-run-"+job.id),actions=document.getElementById("job-actions-"+job.id);
        if(status)status.innerHTML=job.html;
        if(lastRun)lastRun.innerHTML=job.last_run_html;
        if(actions){actions.innerHTML=job.actions_html;if(window.htmx)htmx.process(actions);}
      });
      var operations=document.getElementById("operational-status");
      if(operations&&data.operations_html)operations.outerHTML=data.operations_html;
    }
    dashboard=document.getElementById("dashboard-jobs");
    if(dashboard&&data.dashboard_html){dashboard.outerHTML=data.dashboard_html;dashboard=document.getElementById("dashboard-jobs");}
  }
  function poll(){
    if(stopped||!statusUrl)return;
    fetch(statusUrl,{headers:{"Accept":"application/json"}})
      .then(function(response){return response.ok?response.json():Promise.reject(new Error("status"));})
      .then(function(data){
        apply(data);
        if(!stopped)window.setTimeout(poll,data.active?2000:10000);
      }).catch(function(){if(!stopped)window.setTimeout(poll,5000);});
  }
  if(window.EventSource){
    source=new EventSource(eventUrl);
    source.addEventListener("jobs",function(event){errorCount=0;apply(JSON.parse(event.data));});
    source.addEventListener("ping",function(){errorCount=0;});
    source.onopen=function(){errorCount=0;};
    source.onerror=function(){errorCount+=1;if(errorCount>=3){source.close();source=null;poll();}};
  }else{poll();}
  window.addEventListener("beforeunload",function(){stopped=true;if(source)source.close();});
})();
(function(){
  var form=document.getElementById("catalog-filter-form");
  if(form){
    form.querySelectorAll(".catalog-auto-submit").forEach(function(control){
      control.addEventListener("change",function(){
        if(control.name==="sort"&&control.value==="last_played"){
          var descending=form.querySelector('#direction_desc');if(descending)descending.checked=true;
        }
        form.requestSubmit();
      });
    });
  }
  var results=document.getElementById("catalog-results");
  if(results&&results.dataset.catalogRevisionUrl&&results.dataset.catalogRefreshUrl){
    var revision=results.dataset.catalogRevision;
    function checkRevision(){
      fetch(results.dataset.catalogRevisionUrl,{headers:{"Accept":"application/json"}})
        .then(function(response){return response.ok?response.json():Promise.reject(new Error("revision"));})
        .then(function(data){
          if(data.revision&&data.revision!==revision){
            revision=data.revision;results.dataset.catalogRevision=revision;
            htmx.ajax("GET",results.dataset.catalogRefreshUrl,{target:results,swap:"innerHTML"});
          }
        }).catch(function(){});
    }
    window.setInterval(checkRevision,5000);
  }
  var sidebar=document.querySelector(".catalog-sidebar");
  if(!sidebar)return;
  var key="euvieouvi.catalog.type-order",dragged=null;
  function saveOrder(){try{localStorage.setItem(key,JSON.stringify(Array.from(sidebar.querySelectorAll("[data-catalog-kind]")).map(function(link){return link.dataset.catalogKind;})));}catch(error){}}
  function moveAt(item,x,y){
    if(!dragged||dragged===item)return;
    var box=item.getBoundingClientRect(),horizontal=getComputedStyle(sidebar).flexDirection==="row";
    var after=horizontal?x>box.left+box.width/2:y>box.top+box.height/2;
    sidebar.insertBefore(dragged,after?item.nextSibling:item);
  }
  try{
    var order=JSON.parse(localStorage.getItem(key)||"[]");
    order.forEach(function(kind){var item=sidebar.querySelector('[data-catalog-kind="'+kind+'"]');if(item)sidebar.appendChild(item);});
  }catch(error){}
  sidebar.querySelectorAll("[data-catalog-kind]").forEach(function(item){
    item.addEventListener("dragstart",function(){dragged=item;item.classList.add("catalog-dragging");});
    item.addEventListener("dragend",function(){
      item.classList.remove("catalog-dragging");dragged=null;
      saveOrder();
    });
    item.addEventListener("dragover",function(event){
      event.preventDefault();if(!dragged||dragged===item)return;
      moveAt(item,event.clientX,event.clientY);
    });
    item.addEventListener("keydown",function(event){
      if(!event.altKey||!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(event.key))return;
      event.preventDefault();var previous=["ArrowLeft","ArrowUp"].includes(event.key),sibling=previous?item.previousElementSibling:item.nextElementSibling;
      if(!sibling)return;
      sidebar.insertBefore(item,previous?sibling:sibling.nextSibling);saveOrder();item.focus();
    });
    item.addEventListener("touchstart",function(){dragged=item;},{passive:true});
    item.addEventListener("touchmove",function(event){
      var touch=event.touches[0],target=document.elementFromPoint(touch.clientX,touch.clientY);
      var over=target&&target.closest("[data-catalog-kind]");if(over&&over!==dragged){event.preventDefault();moveAt(over,touch.clientX,touch.clientY);}
    },{passive:false});
    item.addEventListener("touchend",function(){if(dragged){saveOrder();dragged=null;}});
  });
})();
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
