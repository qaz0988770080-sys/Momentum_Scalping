using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.ExceptionServices;
namespace MomentumDiagnostics {
public sealed class ExceptionInfo {public string Kind;public string HResult;public string Source;public string Member;}
public sealed class ConnectionInfo {public bool Attached,ConnectedSeen,DisconnectedSeen,LogonSeen,RecoverySeen;public int LogonResult,BrokerCode,RecoveryCode;public string ResultName="";public string AttachError="";}
public sealed class ManagerInfo {public bool PrimaryLoggedIn,PrimaryRecovering;public string PrimaryRecovery="";public int IdentityCount,IdentityObjectCount,IdentityAttempts;public string[] IdentityStates=new string[0];}
public static class ExceptionRecorder {
static object observed;static ConnectionInfo connectionInfo=new ConnectionInfo();
static readonly Queue<ExceptionInfo> Items=new Queue<ExceptionInfo>();static bool started;[ThreadStatic]static bool inside;
public static void Start(){lock(Items){if(started)return;started=true;AppDomain.CurrentDomain.FirstChanceException+=Observe;}}
static void Observe(object sender,FirstChanceExceptionEventArgs args){if(inside)return;inside=true;try{
Exception e=args.Exception;string kind=e.GetType().FullName;

string source="",member="";var frames=new StackTrace(e,false).GetFrames();if(frames!=null)foreach(var frame in frames){var method=frame.GetMethod();if(method==null||method.DeclaringType==null)continue;string name=method.DeclaringType.Assembly.GetName().Name;if(name.StartsWith("MLApi_")||name=="MasterFutTradeCS"||name=="MasterSpeedyAPI"){source=name;member=method.DeclaringType.FullName+"."+method.Name;break;}}
if(source.Length==0)return;
var item=new ExceptionInfo{Kind=kind,HResult=unchecked((uint)e.HResult).ToString("X8"),Source=source,Member=member};lock(Items){while(Items.Count>=32)Items.Dequeue();Items.Enqueue(item);}
}catch{}finally{inside=false;}}
public static ExceptionInfo[] Take(){lock(Items){var result=Items.ToArray();Items.Clear();return result;}}
public static void ObserveConnection(object manager){try{if(manager==null)return;var field=manager.GetType().GetField("apiSpeedy",BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public);var value=field==null?null:field.GetValue(manager);if(value==null||Object.ReferenceEquals(observed,value))return;observed=value;connectionInfo=new ConnectionInfo();var connection=(SpeedyAPI.OrderConnection)value;
connection.OnConnected+=()=>{lock(Items){connectionInfo.ConnectedSeen=true;}};
connection.OnDisconnected+=()=>{lock(Items){connectionInfo.DisconnectedSeen=true;}};
connection.OnLogonReply+=(string ignoredIdentity,SpeedyAPI.LogonResultEnum result,int code)=>{lock(Items){connectionInfo.LogonSeen=true;connectionInfo.LogonResult=(int)result;connectionInfo.ResultName=result.ToString();connectionInfo.BrokerCode=code;}};
connection.OnRecoverFinished+=(int code)=>{lock(Items){connectionInfo.RecoverySeen=true;connectionInfo.RecoveryCode=code;}};
connectionInfo.Attached=true;
}catch(Exception ex){connectionInfo.AttachError=ex.GetType().Name+":"+unchecked((uint)ex.HResult).ToString("X8");}}
public static ConnectionInfo ConnectionSnapshot(){lock(Items){return new ConnectionInfo{Attached=connectionInfo.Attached,ConnectedSeen=connectionInfo.ConnectedSeen,DisconnectedSeen=connectionInfo.DisconnectedSeen,LogonSeen=connectionInfo.LogonSeen,RecoverySeen=connectionInfo.RecoverySeen,LogonResult=connectionInfo.LogonResult,BrokerCode=connectionInfo.BrokerCode,RecoveryCode=connectionInfo.RecoveryCode,ResultName=connectionInfo.ResultName,AttachError=connectionInfo.AttachError};}}
public static ManagerInfo ManagerSnapshot(object manager){var result=new ManagerInfo();if(manager==null)return result;var flags=BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public;var type=manager.GetType();var logged=type.GetField("fTradeConnStu",flags);result.PrimaryLoggedIn=logged!=null&&(bool)logged.GetValue(manager);var recovery=type.GetField("fTradeRcvStu",flags);result.PrimaryRecovery=recovery==null?"":recovery.GetValue(manager).ToString();var mapField=type.GetField("fMapIdNoOrderConn",flags);var map=mapField==null?null:mapField.GetValue(manager);if(map!=null){var values=map.GetType().GetProperty("Values").GetValue(map,null) as System.Collections.IEnumerable;var states=new List<string>();foreach(var value in values){result.IdentityCount++;var ct=value.GetType();var obj=ct.GetField("apiSpeedy",flags).GetValue(value);if(obj!=null)result.IdentityObjectCount++;result.IdentityAttempts+=(int)ct.GetField("fConnCnt",flags).GetValue(value);states.Add(ct.GetField("fConnStu",flags).GetValue(value).ToString());}result.IdentityStates=states.ToArray();}return result;}
public static bool HasConnectionObject(object manager){if(manager==null)return false;var field=manager.GetType().GetField("apiSpeedy",BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public);return field!=null&&field.GetValue(manager)!=null;}
}
}
