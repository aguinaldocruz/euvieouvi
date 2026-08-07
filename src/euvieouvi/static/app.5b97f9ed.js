document.body.addEventListener("htmx:configRequest", function (event) {
  const token = document.querySelector('meta[name="csrf-token"]');
  if (token) event.detail.headers["X-CSRF-Token"] = token.content;
});
(function(){
  var btn=document.getElementById("themeToggle"),key="euvieouvi.theme";
  function apply(t){document.documentElement.setAttribute("data-theme",t);document.documentElement.style.colorScheme=t;try{localStorage.setItem(key,t)}catch(e){} if(btn) btn.textContent=t==="dark"?"◑":"◐";}
  if(btn){btn.addEventListener("click",function(){var cur=document.documentElement.getAttribute("data-theme")==="dark"?"dark":"light";apply(cur==="dark"?"light":"dark");}); var cur=document.documentElement.getAttribute("data-theme"); if(cur) apply(cur);}
})();
