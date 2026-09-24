module alu_decoder ( inst, alusel_out );
input [31:0] inst;
output [4:0] alusel_out;
wire   inst_30, inst_14, inst_13, inst_12, n27, n28, n29, n30, n31, n32, n33,
n34, n35, n36, n37, n38, n39, n40, n41, n42, n43, n44, n45, n46, n47,
n48, n49, n50, n51, n52, n53, n54;
assign inst_30 = inst[30];
assign inst_14 = inst[14];
assign inst_13 = inst[13];
assign inst_12 = inst[12];
INVxp33_ASAP7_75t_R U34 ( .A(inst[5]), .Y(n48) );
INVxp33_ASAP7_75t_R U35 ( .A(inst_14), .Y(n27) );
OAI221xp5_ASAP7_75t_R U36 ( .A1(inst[5]), .A2(inst_14), .B1(n48), .B2(n27),
.C(inst_13), .Y(n33) );
NOR3xp33_ASAP7_75t_R U37 ( .A(inst_12), .B(inst_13), .C(n27), .Y(n35) );
INVxp33_ASAP7_75t_R U38 ( .A(inst_12), .Y(n47) );
NOR3xp33_ASAP7_75t_R U39 ( .A(inst_14), .B(inst[5]), .C(n47), .Y(n34) );
INVxp33_ASAP7_75t_R U40 ( .A(inst_13), .Y(n49) );
AOI22xp33_ASAP7_75t_R U41 ( .A1(inst[5]), .A2(n35), .B1(n34), .B2(n49), .Y(
n32) );
NAND2xp33_ASAP7_75t_R U42 ( .A(inst_30), .B(n49), .Y(n37) );
NAND2xp33_ASAP7_75t_R U43 ( .A(inst[5]), .B(inst_13), .Y(n28) );
NAND4xp25_ASAP7_75t_R U44 ( .A(inst_12), .B(inst_14), .C(n37), .D(n28), .Y(
n52) );
NAND2xp33_ASAP7_75t_R U45 ( .A(inst[0]), .B(inst[1]), .Y(n29) );
NOR3xp33_ASAP7_75t_R U46 ( .A(n29), .B(inst[3]), .C(inst[6]), .Y(n30) );
NAND2xp33_ASAP7_75t_R U47 ( .A(inst[4]), .B(n30), .Y(n50) );
INVxp33_ASAP7_75t_R U48 ( .A(n50), .Y(n43) );
INVxp33_ASAP7_75t_R U49 ( .A(inst[2]), .Y(n41) );
NAND2xp33_ASAP7_75t_R U50 ( .A(n43), .B(n41), .Y(n31) );
AOI31xp33_ASAP7_75t_R U51 ( .A1(n33), .A2(n32), .A3(n52), .B(n31), .Y(
alusel_out[2]) );
NAND2xp33_ASAP7_75t_R U52 ( .A(inst_14), .B(inst_13), .Y(n36) );
NAND3xp33_ASAP7_75t_R U53 ( .A(inst_14), .B(inst_12), .C(inst_30), .Y(n42)
);
AOI31xp33_ASAP7_75t_R U54 ( .A1(n41), .A2(n36), .A3(n42), .B(n48), .Y(n45)
);
AND2x2_ASAP7_75t_R U55 ( .A(n43), .B(n45), .Y(alusel_out[4]) );
NOR2xp33_ASAP7_75t_R U56 ( .A(inst_13), .B(inst_30), .Y(n46) );
AOI211xp5_ASAP7_75t_R U57 ( .A1(inst_12), .A2(n46), .B(n35), .C(n34), .Y(n40) );
OAI22xp33_ASAP7_75t_R U58 ( .A1(inst_14), .A2(n37), .B1(n36), .B2(n47), .Y(
n38) );
OAI21xp33_ASAP7_75t_R U59 ( .A1(inst[2]), .A2(n38), .B(inst[5]), .Y(n39) );
O2A1O1Ixp33_ASAP7_75t_R U60 ( .A1(n40), .A2(inst[2]), .B(n39), .C(n50), .Y(
alusel_out[1]) );
OA211x2_ASAP7_75t_R U61 ( .A1(n42), .A2(inst_13), .B(n41), .C(n48), .Y(n44)
);
OAI21xp33_ASAP7_75t_R U62 ( .A1(n45), .A2(n44), .B(n43), .Y(alusel_out[3])
);
AOI322xp5_ASAP7_75t_R U63 ( .A1(n48), .A2(n47), .A3(inst_13), .B1(inst[5]),
.B2(inst_12), .C1(n46), .C2(inst[5]), .Y(n54) );
AOI221xp5_ASAP7_75t_R U64 ( .A1(inst[5]), .A2(n49), .B1(n48), .B2(inst_13),
.C(inst_12), .Y(n51) );
AOI211xp5_ASAP7_75t_R U65 ( .A1(inst_14), .A2(n51), .B(inst[2]), .C(n50),
.Y(n53) );
OAI211xp5_ASAP7_75t_R U66 ( .A1(inst_14), .A2(n54), .B(n53), .C(n52), .Y(
alusel_out[0]) );
endmodule