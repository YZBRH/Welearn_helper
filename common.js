const generateCipherText = (password) => {
    let T0 = Date.now();
    let encoder = new TextEncoder();
    let P = encoder.encode(password);
    let V = (T0 >> 16) & 0xFF;
    for (let i = 0; i < P.length; i++) {
        V ^= P[i];
    }
    let remainder = V % 100;
    let T1 = Math.floor(T0 / 100) * 100 + remainder;
    let P1 = Array.from(P, byte => ('0' + (byte & 0xFF).toString(16)).slice(-2)).join('');
    let S = T1 + '*' + P1;
    let S_encoded = encoder.encode(S);
    let E = btoa(String.fromCharCode.apply(null, S_encoded));
    return [E,T1];
}