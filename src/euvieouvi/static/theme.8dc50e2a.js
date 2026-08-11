(function(){
  var root=document.documentElement,key="euvieouvi.theme",stored=null;
  try{stored=localStorage.getItem(key);}catch(e){}
  var configured=root.getAttribute("data-default-theme");
  var system=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
  var theme=stored==="light"||stored==="dark"?stored:configured==="light"||configured==="dark"?configured:system;
  root.setAttribute("data-theme",theme);
  root.style.colorScheme=theme;
})();
